"""Regression tests for turn errors stored outside the Antigravity JSONL export."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from sesskit import titles
from sesskit.adapters import get_adapter
from sesskit.parsers import antigravity as ag


def _varint(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    return bytes([(n & 127) | 128]) + _varint(n >> 7)


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, bytes):
        return _varint(number * 8 + 2) + _varint(len(value)) + value
    return _varint(number * 8) + _varint(value)


def _error_payload(text: str = "Individual quota reached") -> bytes:
    details = _field(1, text.encode()) + _field(7, 429) + _field(4, b"private transport detail")
    return (_field(1, 17) + _field(4, 3) + _field(5, _field(1, _field(1, 1795692969)))
            + _field(24, _field(3, details)))


def _paths(root: Path) -> tuple[Path, Path, dict]:
    path = root / "brain/session/.system_generated/logs/transcript_full.jsonl"
    path.parent.mkdir(parents=True)
    db = root / "conversations/session.db"
    db.parent.mkdir()
    return path, db, {"id": "session", "source": "antigravity", "path": str(path)}


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INT, "
                 "status INT, error_details BLOB, step_payload BLOB)")
    conn.commit()


def _insert(conn: sqlite3.Connection, idx: int, step_type: int, payload: bytes = b"") -> None:
    conn.execute("INSERT INTO steps VALUES (?, ?, 3, NULL, ?)", (idx, step_type, payload))
    conn.commit()


def _append(path: Path, *rows: tuple[int, str, str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for idx, kind, text, status in rows:
            handle.write(json.dumps({"step_index": idx, "type": kind, "content": text,
                                     "status": status, "source": "USER_EXPLICIT"}) + "\n")


def test_database_only_commits_reach_open_and_resumed_readers(tmp_path: Path) -> None:
    path, db, session = _paths(tmp_path)
    _append(path, (0, "USER_INPUT", "Question", "DONE"))
    adapter = get_adapter("antigravity")
    with closing(sqlite3.connect(db)) as conn:
        _init(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        reader = adapter.open_reader(session)
        first = reader.poll()
        assert first.outcome.status == "pending"

        _insert(conn, 1, 132, _error_payload())  # An error-looking tool step is not a turn error.
        _insert(conn, 2, 17, b"\xff")  # Corrupt record must not hide the valid next step.
        assert not reader.poll().reset
        assert reader.full_parses == 1
        main_mtime = db.stat().st_mtime_ns
        _insert(conn, 3, 17, _error_payload())
        assert db.stat().st_mtime_ns == main_mtime
        result = reader.poll()
        assert result.reset and result.outcome.status == "aborted"
        assert result.outcome.error.kind == "quota_exhausted"
        assert result.outcome.error.http_status == 429
        assert result.outcome.evidence.record == "steps:3"
        assert result.events[-1].text == "Individual quota reached"
        assert result.events[-1].ts == 1795692969
        assert not reader.poll().events
        assert reader.full_parses == 2

        resumed = adapter.open_reader(session, first.cursor).poll()
        assert resumed.reset and resumed.outcome == result.outcome
        resumed_current = adapter.open_reader(session, result.cursor)
        assert not resumed_current.poll().reset
        assert resumed_current.full_parses == 0
        assert resumed_current.page().events == reader.page().events
        info = ag._build_session_info("session", str(path), str(db), {"session": {}})
        assert info["status_tag"] == titles.STATUS_ABORTED
        assert info["last_agent_msg"] == "Individual quota reached"
        assert info["terminal_error_kind"] == "quota_exhausted"
        assert info["completion_id"]

        # A successful subsequent turn supersedes the list error. A late JSONL
        # copy of the error must not duplicate it or change the outcome.
        _insert(conn, 4, 1)
        _append(path, (4, "USER_INPUT", "Try again", "DONE"),
                (5, "PLANNER_RESPONSE", "Finished", "DONE"),
                (3, "ERROR_MESSAGE", "Long duplicate report", "ERROR"))
        recovered = reader.poll()
        assert not recovered.reset
        assert recovered.outcome.status == "done"
        snapshot = adapter.load_activity(session)
        assert snapshot.events == reader.page().events
        assert [e.text for e in snapshot.events] == [
            "Question", "Individual quota reached", "Try again", "Finished",
        ]
        assert ag._build_session_info("session", str(path), str(db), {"session": {}})["status_tag"] == titles.STATUS_DONE


def test_db_only_history_does_not_claim_old_error_is_current(tmp_path: Path) -> None:
    _, db, session = _paths(tmp_path)
    session["path"] = str(db)
    adapter = get_adapter("antigravity")
    with closing(sqlite3.connect(db)) as conn:
        _init(conn)
        _insert(conn, 3, 17, _error_payload())
        reader = adapter.open_reader(session)
        assert reader.poll().outcome.status == "aborted"
        assert ag._build_session_info("session", None, str(db), {"session": {}})["status_tag"] == titles.STATUS_ABORTED
        assert len(adapter.load_conversation(session, include_errors=True)) == 1
        assert not adapter.load_conversation(session)
        _insert(conn, 4, 1)
        assert reader.poll().outcome.status == "unknown"
        assert len(reader.page().events) == 1
        assert ag._build_session_info("session", None, str(db), {"session": {}})["status_tag"] == titles.STATUS_NONE
