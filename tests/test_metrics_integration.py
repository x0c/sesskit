"""Regressions for the joined usage/performance delivery, through public APIs."""

from __future__ import annotations

import base64
import dataclasses
import json
import sqlite3
from unittest import mock

import pytest
from test_performance_cli import _registry, _session, _snapshot_with_records
from test_performance_opencode import _assistant_data, _write_v2_db

from sesskit import cli, load_performance, load_usage, performance
from sesskit.native_jsonl import read_jsonl_snapshot
from sesskit.performance_cli import decode_performance_cursor, encode_performance_cursor


def test_unmatched_performance_selector_is_explicit_empty(capsys):
    snapshot = _snapshot_with_records(performance)
    with mock.patch.object(cli, "default_registry", lambda: _registry([_session()])), \
            mock.patch.object(performance, "load_performance", return_value=snapshot):
        code = cli.dispatch(["performance", "abc12345", "--message-id", "absent"])
    payload = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert payload["state"] == "empty"
    assert payload["matched_records"] == 0
    assert payload["unmatched_records"] == 3
    assert payload["summary"]["latest"] is None
    assert payload["summary"]["coverage"] == "partial"
    assert "selector_matched_zero" in payload["diagnostics"]


@pytest.mark.parametrize("selector", ["--turn-id", "--message-id"])
def test_selector_reports_source_unlinked_records(selector, capsys):
    snapshot = _snapshot_with_records(performance)
    snapshot = dataclasses.replace(snapshot, records=(dataclasses.replace(
        snapshot.records[0], turn_id=None, message_id=None, response_id=None),))
    with mock.patch.object(cli, "default_registry", lambda: _registry([_session()])), \
            mock.patch.object(performance, "load_performance", return_value=snapshot):
        code = cli.dispatch(["performance", "abc12345", selector, "absent", "--detail", "records"])
    payload = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert payload["unlinked_records"] == 1
    assert payload["unmatched_records"] == 0
    assert payload["detail"]["items"] == []


@pytest.mark.parametrize("runtime", ["claude", "codex", "kimi", "pi"])
def test_metric_read_failures_are_unavailable(runtime, tmp_path):
    session = {"source": runtime, "id": "missing", "path": str(tmp_path / "absent.jsonl")}
    assert load_usage(session).state == "unavailable"
    snapshot = load_performance(session)
    assert snapshot.state == "unavailable"
    assert snapshot.coverage == "unknown"
    assert snapshot.records == ()


def test_rate_overflow_does_not_crash_or_become_infinity():
    for count, duration in ((10 ** 400, 1000), (10, 5e-324)):
        record = performance.make_performance_record(
            runtime="example", session_ref="example:s", record="line:1",
            kind="response_elapsed", duration_ms=duration, output_tokens=count, allow_rate=True)
        assert record.output_tokens == count
        assert record.tokens_per_second is None
        assert "rate_unavailable" in record.diagnostics
    record = performance.make_performance_record(
        runtime="example", session_ref="example:s", record="line:1",
        kind="response_elapsed", duration_ms=10 ** 400, output_tokens=10, allow_rate=True)
    assert record.duration_ms is None
    assert "duration_invalid" in record.diagnostics


def test_cursor_rejects_boolean_and_oversized_fields():
    args = {"watermark": "w", "session_ref": "pi:s", "history_view": "all_branches",
            "detail": "records", "turn_id": None, "message_id": None}
    cursor = encode_performance_cursor(**args, offset=0)
    for field in ("off", "v"):
        data = json.loads(base64.urlsafe_b64decode(cursor))
        data[field] = True
        bad = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        with pytest.raises(ValueError, match="malformed"):
            decode_performance_cursor(bad, **args)
    with pytest.raises(ValueError, match="malformed"):
        decode_performance_cursor("x" * 4097, **args)


@pytest.mark.parametrize("change", ["append", "replace", "truncate"])
def test_jsonl_rejects_source_changed_during_read(change, tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"x": 1}\n')
    original_loads = json.loads
    changed = False

    def loads(value):
        nonlocal changed
        if not changed:
            changed = True
            if change == "append":
                with path.open("a") as handle:
                    handle.write('{"y": 2}\n')
            elif change == "truncate":
                path.write_text("")
            else:
                replacement = tmp_path / "replacement.jsonl"
                replacement.write_text('{"y": 2}\n')
                replacement.replace(path)
        return original_loads(value)

    with mock.patch("sesskit.native_jsonl.json.loads", side_effect=loads), \
            pytest.raises(OSError, match="history_changed_during_read"):
        read_jsonl_snapshot(str(path))


def test_sqlite_wal_updates_invalidate_metric_cursors(tmp_path):
    path = tmp_path / "history.db"
    _write_v2_db(path, messages=[("m", "assistant", _assistant_data())])
    session = {"source": "opencode", "id": "ses_1", "path": str(path)}
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        before_usage = load_usage(session)
        before_performance = load_performance(session)
        writer.execute("UPDATE session_message SET data = ? WHERE id = ?",
                       (json.dumps(_assistant_data(output=333)), "m"))
        writer.commit()
        after_usage = load_usage(session)
        after_performance = load_performance(session)
    assert before_usage.state == after_usage.state == "available"
    assert before_usage.watermark != after_usage.watermark
    assert before_performance.watermark != after_performance.watermark
    assert after_performance.records[0].output_tokens == 333


def test_kimi_summary_uses_latest_native_source_record(tmp_path):
    path = tmp_path / "wire.jsonl"
    rows = [
        {"type": "step.end", "llmStreamDurationMs": 1000, "usage": {"output": 10}},
        {"type": "turn.ended", "durationMs": 2000},
        {"type": "step.end", "llmStreamDurationMs": 1000, "usage": {"output": 20}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    snapshot = load_performance({"source": "kimi", "id": "s", "path": str(path)})
    assert performance.performance_summary(snapshot)["latest"]["record"] == "line:3"


def test_usage_holds_incomplete_line_and_keeps_read_diagnostics(tmp_path):
    path = tmp_path / "history.jsonl"
    row = {"type": "assistant", "message": {"id": "m", "usage": {"output_tokens": 7}}}
    path.write_text("not-json\n" + json.dumps(row))
    session = {"source": "claude", "id": "s", "path": str(path)}
    snapshot = load_usage(session)
    assert snapshot.state == "empty"
    assert snapshot.entries_read == 1
    assert "skipped_malformed:1" in snapshot.diagnostics
    assert "trailing_partial_held" in snapshot.diagnostics
    with path.open("a") as handle:
        handle.write("\n")
    assert load_usage(session).state == "available"
