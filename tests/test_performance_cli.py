"""Performance CLI: envelope, paging, selectors, cursors, error states.

Tests that need sesskit.performance skip until the core worker's module
lands (dependency_not_ready); argument/ref validation runs regardless.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from sesskit import cli
from sesskit.models import make_session_info
from sesskit.registry import ParserRegistry, RuntimeParser


def _session(source="pi", sid="abc12345", path="/tmp/x.jsonl"):
    return make_session_info(
        source=source,
        id=sid,
        short_id=sid[:8],
        cwd="/tmp/demo",
        mtime=1_700_000_000.0,
        time_source="file_mtime",
        event_time=1_700_000_000.0,
        file_mtime=1_700_000_000.0,
        size_bytes=100,
        native_title="Demo",
        fallback_title="Demo",
        status_tag="",
        path=path,
        first_user_msg="hello",
        last_user_msg="hello",
        last_agent_msg="world",
    )


def _registry(sessions, runtime_id="pi", display="Pi"):
    def scan(limit=50, keep_ids=None, cwd_filter=None):
        return list(sessions)[:limit]

    def load(session):
        from sesskit.models import ConversationMessage

        return [ConversationMessage(role="user", text="hello", timestamp=1.0)]

    return ParserRegistry([RuntimeParser(id=runtime_id, display_name=display, _scan=scan, _load=load)])


def _run_cli(argv, sessions, capsys, runtime_id="pi"):
    with mock.patch.object(cli, "default_registry", lambda: _registry(sessions, runtime_id)):
        code = cli.dispatch(argv)
    out = json.loads(capsys.readouterr().out)
    return code, out


def _needs_core():
    return pytest.importorskip("sesskit.performance")


def _snapshot_with_records(perf, *, count=3, watermark="gen-a"):
    from sesskit.models import Evidence

    records = tuple(
        perf.make_performance_record(
            runtime="pi",
            session_ref="pi:abc12345",
            record=f"line:{index + 1}",
            kind="response_elapsed",
            scope_kind="message",
            scope_id=f"m{index + 1}",
            duration_ms=1000.0,
            output_tokens=10 * (index + 1),
            output_semantics="provider_output",
            evidence=(Evidence(origin="native", field="durationMs", record=f"line:{index + 1}"),),
            allow_rate=True,
            clock="monotonic",
            message_id=f"m{index + 1}",
            response_id=f"r{index + 1}",
        )
        for index in range(count)
    )
    return perf.PerformanceSnapshot(
        state="available",
        runtime="pi",
        session_ref="pi:abc12345",
        history_view="all_branches",
        records=records,
        entries_read=count,
        watermark=watermark,
        observed_operations=count,
        coverage="partial",
    )


def test_describe_lists_performance(capsys):
    with mock.patch.object(cli, "default_registry", lambda: _registry([_session()])):
        code = cli.dispatch(["describe"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in out["data"]["commands"]}
    assert "performance" in names


def test_describe_performance_spec(capsys):
    with mock.patch.object(cli, "default_registry", lambda: _registry([_session()])):
        code = cli.dispatch(["describe", "performance"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    flags = {flag for arg in out["data"]["args"] for flag in arg["flags"]}
    assert {"--detail", "--limit", "--cursor", "--view", "--turn-id", "--message-id"} <= flags


def test_summary_rejects_limit_and_cursor(capsys):
    code, out = _run_cli(["performance", "abc12345", "--limit", "5", "--compact"], [_session()], capsys)
    assert code == 2
    assert out["error"]["code"] == "usage_error"
    code, out = _run_cli(["performance", "abc12345", "--cursor", "x", "--compact"], [_session()], capsys)
    assert code == 2


def test_selectors_mutually_exclusive(capsys):
    code, _ = _run_cli(
        ["performance", "abc12345", "--turn-id", "t", "--message-id", "m", "--compact"], [_session()], capsys
    )
    assert code == 2


def test_records_limit_bounds(capsys):
    for bad in ("0", "1001"):
        code, _ = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--limit", bad, "--compact"], [_session()], capsys
        )
        assert code == 2


def test_missing_and_ambiguous_ref(capsys):
    code, out = _run_cli(["performance", "nosuchref", "--compact"], [_session()], capsys)
    assert code == 3
    assert out["error"]["code"] == "not_found"
    sessions = [_session(sid="abc11111"), _session(sid="abc22222")]
    code, out = _run_cli(["performance", "abc", "--compact"], sessions, capsys)
    assert code == 5
    assert out["error"]["code"] == "ambiguous"


def test_summary_envelope_bounded(capsys):
    perf = _needs_core()
    snapshot = _snapshot_with_records(perf)
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        code, out = _run_cli(["performance", "abc12345", "--compact"], [_session()], capsys)
    assert code == 0
    assert out["ok"] is True
    data = out["data"]
    assert data["schema"] == "sesskit.performance/v1"
    assert data["summary"]["record_count"] == 3
    assert data["summary"]["rate_count"] == 3
    # Default summary carries at most one latest record, never a full dump.
    assert "detail" not in data
    latest = data["summary"]["latest"]
    assert latest is not None and latest["kind"] == "response_elapsed"
    assert latest["tokens_per_second"] == pytest.approx(30.0)


def test_detail_paging_roundtrip(capsys):
    perf = _needs_core()
    snapshot = _snapshot_with_records(perf)
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        code, out = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--limit", "2", "--compact"], [_session()], capsys
        )
    assert code == 0
    detail = out["data"]["detail"]
    assert (detail["returned"], detail["total"], detail["has_more"]) == (2, 3, True)
    cursor = detail["cursor"]
    assert cursor
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        code, out = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--limit", "2", "--cursor", cursor, "--compact"],
            [_session()],
            capsys,
        )
    assert code == 0
    second = out["data"]["detail"]
    assert second["returned"] == 1 and second["has_more"] is False
    assert second["items"][0]["performance_id"] != detail["items"][0]["performance_id"]


def test_stale_and_malformed_cursor(capsys):
    perf = _needs_core()
    first = _snapshot_with_records(perf, watermark="gen-a")
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": first):
        _, out = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--limit", "1", "--compact"], [_session()], capsys
        )
    cursor = out["data"]["detail"]["cursor"]
    changed = _snapshot_with_records(perf, watermark="gen-b")
    with (
        mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": changed),
        mock.patch.object(cli, "default_registry", lambda: _registry([_session()])),
    ):
        code = cli.dispatch(["performance", "abc12345", "--detail", "records", "--cursor", cursor, "--compact"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["error"]["code"] == "stale_cursor"
    assert out["error"]["next_commands"] == ["sesskit performance abc12345"]
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": first):
        code, out = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--cursor", "bogus", "--compact"], [_session()], capsys
        )
    assert code == 2
    assert out["error"]["code"] == "usage_error"


def test_cross_scope_cursor_refused(capsys):
    perf = _needs_core()
    snapshot = _snapshot_with_records(perf)
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        _, out = _run_cli(
            ["performance", "abc12345", "--detail", "records", "--limit", "1", "--compact"], [_session()], capsys
        )
    cursor = out["data"]["detail"]["cursor"]
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        code, out = _run_cli(
            [
                "performance",
                "abc12345",
                "--detail",
                "records",
                "--limit",
                "1",
                "--cursor",
                cursor,
                "--message-id",
                "m1",
                "--compact",
            ],
            [_session()],
            capsys,
        )
    assert code == 1
    assert out["error"]["code"] == "stale_cursor"


def test_selector_updates_summary_and_detail(capsys):
    perf = _needs_core()
    snapshot = _snapshot_with_records(perf)
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": snapshot):
        code, out = _run_cli(["performance", "abc12345", "--message-id", "m2", "--compact"], [_session()], capsys)
    assert code == 0
    assert out["data"]["matched_records"] == 1
    assert out["data"]["summary"]["record_count"] == 1
    assert out["data"]["summary"]["latest"]["message_id"] == "m2"


def test_unavailable_is_error_and_unsupported_is_explicit(capsys):
    perf = _needs_core()
    missing = perf.PerformanceSnapshot(
        state="unavailable",
        runtime="pi",
        session_ref="pi:abc12345",
        history_view="all_branches",
        records=(),
        entries_read=0,
        watermark=None,
        observed_operations=0,
        coverage="not_recorded",
        diagnostics=("history_unreadable",),
    )
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": missing):
        code, out = _run_cli(["performance", "abc12345", "--compact"], [_session()], capsys)
    assert code == 1
    assert out["error"]["code"] == "load_failed"
    unsupported = perf.unsupported_performance_snapshot(
        runtime="cursor", session_ref="cursor:c1", history_view="all_branches"
    )
    with mock.patch.object(perf, "load_performance", lambda session, history_view="all_branches": unsupported):
        code, out = _run_cli(
            ["performance", "c1", "--compact"], [_session(source="cursor", sid="c1")], capsys, runtime_id="cursor"
        )
    assert code == 0
    assert out["data"]["state"] == "unsupported"
    assert out["data"]["summary"]["coverage"] == "unsupported"
    assert out["data"]["summary"]["latest"] is None
