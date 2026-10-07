"""Settings-only restores must not promote historical conversations."""

import json
import os
from datetime import datetime, timezone
from unittest import mock

import pytest

from sesskit import titles
from sesskit.parsers import codex

OLD = "2026-09-28T10:04:35.514Z"
NEW = "2026-10-07T05:02:35.379Z"
UUID = "00000000-0000-4000-8000-000000000001"


def _stamp(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _row(kind, subtype, stamp=OLD, **payload):
    return {"timestamp": stamp, "type": kind, "payload": {"type": subtype, **payload}}


def _history(tmp_path, suffix=(), old=OLD):
    path = tmp_path / f"rollout-2026-09-28T18-04-35-{UUID}.jsonl"
    rows = [
        _row("session_meta", "session_meta", old, id=UUID, cwd=str(tmp_path)),
        _row("event_msg", "user_message", old, message="Explain the example"),
        _row("event_msg", "task_complete", old, turn_id="turn-1", last_agent_message="Done"),
        *suffix,
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    os.utime(path, (_stamp(NEW), _stamp(NEW)))
    return path


@pytest.mark.parametrize("suffix", [
    [_row("event_msg", "thread_settings_applied", NEW, thread_settings={"model": "example"})],
    [_row("event_msg", "token_count", NEW)],
    [_row("token_usage_record", "accounting", NEW)],
    [_row("turn_context", "context", NEW)],
])
def test_metadata_append_preserves_conversation_clock_and_terminal_identity(tmp_path, suffix):
    path = _history(tmp_path)
    before = codex._build_session_info(str(path), {})
    with path.open("a") as handle:
        for row in suffix:
            handle.write(json.dumps(row) + "\n")
    os.utime(path, (_stamp(NEW), _stamp(NEW)))
    after = codex._build_session_info(str(path), {})
    assert after["mtime"] == _stamp(OLD)
    assert after["event_time"] == _stamp(OLD)
    assert after["file_mtime"] == _stamp(NEW)
    assert after["time_source"] == "event_time_metadata"
    assert after["status_tag"] == before["status_tag"] == titles.STATUS_DONE
    assert after["completion_id"] == before["completion_id"]


def test_short_settings_append_across_midnight_does_not_change_day(tmp_path):
    old = "2026-10-06T23:55:00Z"
    new = "2026-10-07T00:05:00Z"
    path = _history(tmp_path, [_row("event_msg", "thread_settings_applied", new)], old)
    os.utime(path, (_stamp(new), _stamp(new)))
    info = codex._build_session_info(str(path), {})
    assert datetime.fromtimestamp(info["mtime"], timezone.utc).date().isoformat() == "2026-10-06"


def test_metadata_eviction_backfills_only_the_activity_clock(tmp_path):
    middle = "2026-10-01T12:34:56Z"
    # More than the 8 KB status tail: terminal evidence must remain unknown,
    # while the clock recovers the latest activity instead of the first prompt.
    suffix = [_row("event_msg", "task_complete", middle, turn_id="turn-2", last_agent_message="Done")]
    suffix += [_row("event_msg", "thread_settings_applied", NEW, padding="x" * 1024) for _ in range(12)]
    path = _history(tmp_path, suffix)
    info = codex._build_session_info(str(path), {})
    assert info["mtime"] == _stamp(middle)
    assert info["status_tag"] == titles.STATUS_NONE
    assert not info["completion_id"]


@pytest.mark.parametrize("activity", [
    _row("event_msg", "user_message", NEW, message="Continue"),
    _row("event_msg", "task_started", NEW, turn_id="turn-2"),
    _row("response_item", "function_call", NEW, name="example", call_id="call-2", arguments="{}"),
    _row("response_item", "function_call_output", NEW, call_id="call-2", output="ok"),
    _row("response_item", "message", NEW, role="assistant", content=[{"type": "output_text", "text": "New reply"}]),
    _row("event_msg", "turn_aborted", NEW, turn_id="turn-2"),
])
def test_genuine_new_activity_advances_the_clock(tmp_path, activity):
    path = _history(tmp_path, [activity, _row("event_msg", "thread_settings_applied", NEW)])
    info = codex._build_session_info(str(path), {})
    assert info["mtime"] == _stamp(NEW)


def test_public_scan_orders_by_activity_after_settings_restore(tmp_path):
    restored = _history(tmp_path, [_row("event_msg", "thread_settings_applied", NEW)])
    other_id = "00000000-0000-4000-8000-000000000002"
    recent = tmp_path / f"rollout-2026-10-01T12-34-56-{other_id}.jsonl"
    rows = [
        _row("session_meta", "session_meta", NEW, id=other_id, cwd=str(tmp_path)),
        _row("event_msg", "user_message", NEW, message="New conversation"),
    ]
    recent.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    os.utime(recent, (_stamp(NEW), _stamp(NEW)))
    with (
        mock.patch.object(codex, "SESSIONS_DIR", str(tmp_path)),
        mock.patch.object(codex, "SESSION_INDEX", str(tmp_path / "absent-index")),
        mock.patch.object(codex, "live_pid_snapshot", return_value=()),
    ):
        rows = codex.scan_sessions(limit=20)
    assert [row["id"] for row in rows] == [other_id, UUID]
    assert rows[1]["mtime"] == _stamp(OLD)
    assert restored.exists()
