"""List-record memo: unchanged sessions are not rebuilt on repeated scans.

A long-lived scanner calls ``scan_sessions`` again whenever any session of a
runtime changes. Pi files and OpenCode v2 sessions whose native source did not
change must reuse their built record; a changed source must be rebuilt, and a
record handed to a caller must never alias the memo.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit import titles
from sesskit.parsers import opencode as oc
from sesskit.parsers import pi as scan_pi
from sesskit.parsers.common import MEMO_MISS, HostExtension, ListRecordMemo


class ListRecordMemoTests(unittest.TestCase):
    def test_version_mismatch_misses_and_values_are_copied(self) -> None:
        memo = ListRecordMemo()
        record = {"id": "a", "nested": {"k": 1}}
        memo.put("key", ("v1",), record)
        record["nested"]["k"] = 2
        self.assertIs(memo.get("key", ("v2",)), MEMO_MISS)
        hit = memo.get("key", ("v1",))
        self.assertEqual(hit, {"id": "a", "nested": {"k": 1}})
        hit["nested"]["k"] = 3
        self.assertEqual(memo.get("key", ("v1",))["nested"]["k"], 1)

    def test_none_results_are_memoized(self) -> None:
        memo = ListRecordMemo()
        memo.put("key", 1, None)
        self.assertIsNone(memo.get("key", 1))

    def test_bounded_lru(self) -> None:
        memo = ListRecordMemo(limit=2)
        memo.put("a", 1, "A")
        memo.put("b", 1, "B")
        memo.get("a", 1)
        memo.put("c", 1, "C")
        self.assertIs(memo.get("b", 1), MEMO_MISS)
        self.assertEqual(memo.get("a", 1), "A")

    def test_host_transform_is_part_of_the_version(self) -> None:
        plain = HostExtension(name="h", excerpt_preprocess=str.upper)
        other = HostExtension(name="h", excerpt_preprocess=str.lower)
        self.assertNotEqual(ListRecordMemo.host_key(plain), ListRecordMemo.host_key(other))
        self.assertNotEqual(ListRecordMemo.host_key(None), ListRecordMemo.host_key(plain))


def _pi_entries(session_id: str, cwd: str, texts: list[str]) -> list[dict]:
    entries: list[dict] = [
        {"type": "session", "id": session_id, "cwd": cwd, "timestamp": "2026-09-29T00:00:00Z"},
    ]
    parent = None
    for index, text in enumerate(texts):
        entry_id = f"m{index}"
        entries.append({
            "id": entry_id,
            "parentId": parent,
            "type": "message",
            "timestamp": f"2026-09-29T00:00:{index + 1:02d}Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        })
        parent = entry_id
    return entries


class PiListMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cwd = str(self.root / "proj")
        os.makedirs(self.cwd)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        for patcher in (
            mock.patch.object(scan_pi, "SESSIONS_DIR", str(self.sessions)),
            mock.patch.object(scan_pi, "live_processes", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        scan_pi._LIST_MEMO.clear()
        self.addCleanup(scan_pi._LIST_MEMO.clear)

    def _write(self, session_id: str, texts: list[str]) -> Path:
        path = self.sessions / f"2026-09-29T00-00-00-000Z_{session_id}.jsonl"
        path.write_text(
            "".join(json.dumps(e) + "\n" for e in _pi_entries(session_id, self.cwd, texts)),
            encoding="utf-8",
        )
        return path

    def _scan(self) -> dict[str, dict]:
        return {s["id"]: s for s in scan_pi.scan_sessions(limit=10)}

    def test_only_changed_files_are_rebuilt(self) -> None:
        self._write("a", ["first a"])
        self._write("b", ["first b"])
        real = scan_pi._build_session_info
        with mock.patch.object(scan_pi, "_build_session_info", side_effect=real) as build:
            self.assertEqual(set(self._scan()), {"a", "b"})
            self.assertEqual(build.call_count, 2)
            build.reset_mock()
            self._scan()
            self.assertEqual(build.call_count, 0)
            self._write("b", ["first b", "second b"])
            sessions = self._scan()
            self.assertEqual(build.call_count, 1)
        self.assertEqual(sessions["b"]["last_user_msg"], "second b")
        self.assertEqual(sessions["a"]["last_user_msg"], "first a")

    def test_caller_mutation_does_not_leak_into_the_next_scan(self) -> None:
        self._write("a", ["hello"])
        first = self._scan()["a"]
        first["live"] = True
        first["last_user_msg"] = "mutated"
        again = self._scan()["a"]
        self.assertFalse(again["live"])
        self.assertEqual(again["last_user_msg"], "hello")


_V2_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, directory TEXT, title TEXT,
    time_created INTEGER, time_updated INTEGER,
    parent_id TEXT, time_archived INTEGER);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY, directory TEXT NOT NULL, title TEXT,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    parent_id TEXT, time_archived INTEGER);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
    seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


class OpenCodeV2ListMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = os.path.join(tmp.name, "proj")
        os.makedirs(self.cwd)
        self.db_path = os.path.join(tmp.name, "opencode.db")
        self.writer = sqlite3.connect(self.db_path)
        self.addCleanup(self.writer.close)
        self.writer.executescript(_V2_SCHEMA)
        self.writer.execute("PRAGMA journal_mode=WAL")
        for patcher in (
            mock.patch.dict(os.environ, {"OPENCODE_DATA_DIR": tmp.name}),
            mock.patch.object(oc, "live_processes", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for session_id, updated in (("ses_a", 200), ("ses_b", 100)):
            self.writer.execute(
                "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                (session_id, self.cwd, f"title {session_id}", 1, updated, None, None),
            )
            self._message(session_id, 0, "user", 10, {"text": f"ask {session_id}"})
            self._message(
                session_id, 1, "assistant", 11,
                {"content": [{"type": "text", "text": f"reply {session_id}"}], "finish": "stop"},
            )
        self.writer.commit()

    def _message(self, session_id: str, seq: int, kind: str, updated: int, data: dict) -> None:
        self.writer.execute(
            "INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"{session_id}#{seq}", session_id, kind, seq, updated, updated, json.dumps(data)),
        )

    def _scan(self) -> dict[str, dict]:
        return {s["id"]: s for s in oc.scan_sessions(limit=10)}

    def test_only_changed_sessions_are_rebuilt(self) -> None:
        real = oc._build_session_info_v2
        with mock.patch.object(oc, "_build_session_info_v2", side_effect=real) as build:
            self.assertEqual(set(self._scan()), {"ses_a", "ses_b"})
            self.assertEqual(build.call_count, 2)
            build.reset_mock()
            self._scan()
            self.assertEqual(build.call_count, 0)
            # A new turn in one session: only that session is rebuilt, even
            # though the session row itself was not touched.
            self._message("ses_b", 2, "user", 20, {"text": "follow-up"})
            self.writer.commit()
            sessions = self._scan()
            self.assertEqual(build.call_count, 1)
        self.assertEqual(sessions["ses_b"]["status_tag"], titles.STATUS_PENDING)
        self.assertEqual(sessions["ses_b"]["last_user_msg"], "follow-up")
        self.assertEqual(sessions["ses_a"]["status_tag"], titles.STATUS_DONE)

    def test_same_timestamp_rewrite_with_new_length_is_rebuilt(self) -> None:
        self._scan()
        self._message(
            "ses_a", 1, "assistant", 11,
            {"content": [{"type": "text", "text": "a longer edited reply"}], "finish": "stop"},
        )
        self.writer.commit()
        self.assertEqual(self._scan()["ses_a"]["last_agent_msg"], "a longer edited reply")

    def test_deleted_message_is_rebuilt(self) -> None:
        self._scan()
        self.writer.execute("DELETE FROM session_message WHERE id = 'ses_a#1'")
        self.writer.commit()
        self.assertEqual(self._scan()["ses_a"]["status_tag"], titles.STATUS_PENDING)

    def test_caller_mutation_does_not_leak_into_the_next_scan(self) -> None:
        first = self._scan()["ses_a"]
        first["last_agent_msg"] = "mutated"
        self.assertEqual(self._scan()["ses_a"]["last_agent_msg"], "reply ses_a")

    def test_version_query_failure_builds_without_memo(self) -> None:
        with mock.patch.object(oc, "_v2_session_versions", return_value=None):
            self.assertEqual(self._scan()["ses_a"]["last_agent_msg"], "reply ses_a")


if __name__ == "__main__":
    unittest.main()
