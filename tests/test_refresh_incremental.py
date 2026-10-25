"""Parity regressions for memoized refresh and Pi incremental JSONL reuse.

Every refreshed record is compared with the uncached builder over the same
native bytes. No fixture is derived from the parser under test.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from sesskit import titles
from sesskit.parsers import opencode as oc
from sesskit.parsers import pi
from sesskit.parsers.common import HostExtension


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _append_rows(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _header(session_id: str, cwd: str) -> dict:
    return {"type": "session", "id": session_id, "cwd": cwd,
            "timestamp": "2026-10-25T04:00:00.000Z"}


def _user(entry_id: str, parent: str | None, text: str, second: int = 1) -> dict:
    return {
        "type": "message", "id": entry_id, "parentId": parent,
        "timestamp": f"2026-10-25T04:00:{second:02d}.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(
    entry_id: str, parent: str | None, text: str, *,
    stop: str | None = "stop", error: str | None = None, second: int = 2,
) -> dict:
    message: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
    }
    if stop is not None:
        message["stopReason"] = stop
    if error is not None:
        message["errorMessage"] = error
    return {
        "type": "message", "id": entry_id, "parentId": parent,
        "timestamp": f"2026-10-25T04:00:{second:02d}.000Z",
        "message": message,
    }


def _uncached(path: Path, host: HostExtension | None = None) -> dict | None:
    built = pi._build_session_info_from_entries(
        str(path), pi.read_entries(str(path)), host,
    )
    return built[0] if built is not None else None


def _refresh(path: Path, host: HostExtension | None = None) -> dict | None:
    return pi.refresh_session({"source": "pi", "path": str(path)}, host=host)


class _PiBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "2026-10-25T04-00-00-000Z_pi-refresh.jsonl"
        pi._entry_cache_clear()
        pi._LIST_MEMO.clear()
        self.addCleanup(pi._entry_cache_clear)
        self.addCleanup(pi._LIST_MEMO.clear)
        _write_rows(self.path, [
            _header("pi-refresh", str(self.root)),
            _user("u1", None, "first question", 1),
            _assistant("a1", "u1", "first answer", second=2),
            _user("u2", "a1", "second question", 3),
        ])

    def assertParity(self, host: HostExtension | None = None) -> dict:
        refreshed = _refresh(self.path, host)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed, _uncached(self.path, host))
        return refreshed


class PiIncrementalParityTests(_PiBase):
    def test_append_matches_uncached_without_rereading_prefix(self) -> None:
        self.assertParity()
        _append_rows(self.path, [_assistant("a2", "u2", "second answer", second=4)])
        expected = _uncached(self.path)
        with mock.patch.object(pi, "read_entries", side_effect=AssertionError("full reread")):
            refreshed = _refresh(self.path)
        self.assertEqual(refreshed, expected)
        assert refreshed is not None
        self.assertEqual(refreshed["status_tag"], titles.STATUS_DONE)
        self.assertEqual(refreshed["last_agent_msg"], "second answer")

    def test_partial_line_is_not_committed_until_newline(self) -> None:
        self.assertParity()
        row = _assistant("a2", "u2", "partial answer", second=4)
        encoded = json.dumps(row)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded[: len(encoded) // 2])
        self.assertParity()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded[len(encoded) // 2:] + "\n")
        refreshed = self.assertParity()
        self.assertEqual(refreshed["last_agent_msg"], "partial answer")
        self.assertEqual(refreshed["status_tag"], titles.STATUS_DONE)

    def test_same_size_overwrite_rebuilds(self) -> None:
        row = _assistant("a2", "u2", "aaaa", second=4)
        _append_rows(self.path, [row])
        first = self.assertParity()
        self.assertEqual(first["last_agent_msg"], "aaaa")
        before = self.path.stat()
        with self.path.open("r+b") as handle:
            data = handle.read()
            handle.seek(0)
            handle.write(data.replace(b'"aaaa"', b'"bbbb"'))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        refreshed = self.assertParity()
        self.assertEqual(refreshed["last_agent_msg"], "bbbb")
        self.assertEqual(self.path.stat().st_size, before.st_size)

    def test_truncation_and_replacement_rebuild(self) -> None:
        _append_rows(self.path, [_assistant("a2", "u2", "temporary answer", second=4)])
        self.assertParity()
        self.path.write_text(
            "".join(json.dumps(row) + "\n" for row in [
                _header("pi-refresh", str(self.root)),
                _user("u1", None, "replacement question", 1),
            ]),
            encoding="utf-8",
        )
        truncated = self.assertParity()
        self.assertEqual(truncated["status_tag"], titles.STATUS_PENDING)

        replacement = self.root / "replacement.jsonl"
        _write_rows(replacement, [
            _header("pi-refresh", str(self.root)),
            _user("u9", None, "brand new question", 5),
            _assistant("a9", "u9", "brand new answer", second=6),
        ])
        os.replace(replacement, self.path)
        replaced = self.assertParity()
        self.assertEqual(replaced["first_user_msg"], "brand new question")
        self.assertEqual(replaced["status_tag"], titles.STATUS_DONE)

    def test_branch_switch_and_compaction_match_uncached(self) -> None:
        self.assertParity()
        # The new leaf points at the first answer, abandoning the u2 branch.
        _append_rows(self.path, [
            _assistant("a-side", "a1", "side answer", second=7),
        ])
        side = self.assertParity()
        self.assertEqual(side["last_agent_msg"], "side answer")
        self.assertEqual(side["last_user_msg"], "first question")

        _append_rows(self.path, [
            {"type": "compaction", "id": "c1", "parentId": "a-side",
             "timestamp": "2026-10-25T04:00:08.000Z",
             "summary": "compacted context"},
            _user("u3", "c1", "after compaction", 9),
            _assistant("a3", "u3", "post-compaction answer", second=10),
        ])
        compacted = self.assertParity()
        self.assertEqual(compacted["last_agent_msg"], "post-compaction answer")
        self.assertEqual(compacted["status_tag"], titles.STATUS_DONE)
        self.assertTrue(compacted["completion_id"])

    def test_malformed_append_and_native_title_match_uncached(self) -> None:
        self.assertParity()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not valid json\n")
        self.assertParity()
        _append_rows(self.path, [
            {"type": "session_info", "id": "info-1", "parentId": "u2",
             "name": "Native title", "timestamp": "2026-10-25T04:00:11.000Z"},
        ])
        titled = self.assertParity()
        self.assertEqual(titled["native_title"], "Native title")

    def test_missing_terminal_id_and_host_transform_match_uncached(self) -> None:
        _append_rows(self.path, [
            _assistant("a-no-id", "u2", "no stop reason", stop=None, second=4),
        ])
        unknown = self.assertParity()
        self.assertEqual(unknown["status_tag"], titles.STATUS_NONE)
        self.assertEqual(unknown["completion_id"], "")

        upper = HostExtension(name="upper", excerpt_preprocess=str.upper)
        lower = HostExtension(name="lower", excerpt_preprocess=str.lower)
        upper_info = self.assertParity(upper)
        lower_info = self.assertParity(lower)
        self.assertEqual(upper_info["last_agent_msg"], "NO STOP REASON")
        self.assertEqual(lower_info["last_agent_msg"], "no stop reason")

    def test_missing_native_message_ids_match_uncached(self) -> None:
        path = self.root / "legacy-ids.jsonl"
        _write_rows(path, [
            _header("legacy-ids", str(self.root)),
            {"type": "message", "timestamp": "2026-10-25T04:00:01.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "legacy ask"}]}},
            {"type": "message", "timestamp": "2026-10-25T04:00:02.000Z",
             "message": {"role": "assistant", "stopReason": "stop",
                         "content": [{"type": "text", "text": "legacy answer"}]}},
        ])
        refreshed = _refresh(path)
        self.assertEqual(refreshed, _uncached(path))
        assert refreshed is not None
        self.assertEqual(refreshed["status_tag"], titles.STATUS_DONE)
        self.assertEqual(refreshed["last_agent_msg"], "legacy answer")
        self.assertEqual(refreshed["completion_id"], _uncached(path)["completion_id"])

    def test_boundary_rewrite_with_append_rebuilds(self) -> None:
        self.assertParity()
        before = self.path.stat()
        data = self.path.read_bytes()
        # Rewrite the last committed line in place while also appending; only
        # the boundary checksum can catch this grow-with-rewrite case.
        rewritten = data.replace(b'"second question"', b'"rewritten question"')
        self.assertNotEqual(rewritten, data)
        self.path.write_bytes(rewritten)
        _append_rows(self.path, [_assistant("a2", "u2", "after rewrite", second=4)])
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        refreshed = self.assertParity()
        self.assertEqual(refreshed["last_user_msg"], "rewritten question")

    def test_concurrent_refreshes_are_consistent(self) -> None:
        self.assertParity()
        _append_rows(self.path, [_assistant("a2", "u2", "threaded answer", second=4)])
        expected = _uncached(self.path)

        def run(_index: int) -> dict | None:
            return _refresh(self.path)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(64)))
        self.assertEqual(results, [expected] * len(results))

    def test_cache_budget_evicts_and_large_files_stay_uncached(self) -> None:
        other = self.root / "other.jsonl"
        _write_rows(other, [
            _header("other", str(self.root)),
            _user("u1", None, "other question", 1),
            _assistant("a1", "u1", "other answer", second=2),
        ])
        with mock.patch.object(pi, "_ENTRY_CACHE_MAX_BYTES", 4096), \
                mock.patch.object(pi, "_ENTRY_CACHE_MAX_FILES", 1):
            self.assertParity()
            _refresh(other)
            stats = pi._entry_cache_stats()
            self.assertEqual(stats["files"], 1)
            self.assertLessEqual(stats["source_bytes"], stats["limit_bytes"])
            self.assertEqual(_refresh(other), _uncached(other))
        with mock.patch.object(pi, "_ENTRY_CACHE_MAX_BYTES", 100):
            pi._entry_cache_clear()
            _refresh(self.path)
            self.assertEqual(pi._entry_cache_stats()["files"], 0)
            self.assertEqual(_refresh(self.path), _uncached(self.path))

    def test_missing_file_is_none_and_cache_is_dropped(self) -> None:
        self.assertParity()
        self.path.unlink()
        self.assertIsNone(_refresh(self.path))
        self.assertEqual(pi._entry_cache_stats()["files"], 0)


_OC_SCHEMA = """
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


class _OpenCodeBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "opencode.db"
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.writer.executescript(_OC_SCHEMA)
        self.writer.execute("PRAGMA journal_mode=WAL")
        for session_id, updated in (("ses_a", 200), ("ses_b", 100)):
            self.writer.execute(
                "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                (session_id, str(self.root), f"title {session_id}", 1, updated, None, None),
            )
            self._message(session_id, 0, "user", 10, {"text": f"ask {session_id}"})
            self._message(
                session_id, 1, "assistant", 11,
                {"content": [{"type": "text", "text": f"reply {session_id}"}], "finish": "stop"},
            )
        self.writer.commit()
        oc._V2_LIST_MEMO.clear()
        self.addCleanup(oc._V2_LIST_MEMO.clear)

    def _message(self, session_id: str, seq: int, kind: str, updated: int, data: dict) -> None:
        self.writer.execute(
            "INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"{session_id}#{seq}", session_id, kind, seq, updated, updated, json.dumps(data)),
        )

    def _session(self, session_id: str) -> dict:
        return {"source": "opencode", "path": str(self.db), "id": session_id}

    def _uncached(self, session_id: str, host: HostExtension | None = None) -> dict | None:
        conn = oc.connect_ro(str(self.db))
        assert conn is not None
        try:
            row = conn.execute(oc._SESSION_SQL_V2, (session_id,)).fetchone()
            return (
                oc._build_session_info_v2(conn, row, str(self.db), host)
                if row is not None else None
            )
        finally:
            conn.close()

    def assertParity(self, session_id: str, host: HostExtension | None = None) -> dict:
        refreshed = oc.refresh_session(self._session(session_id), host=host)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed, self._uncached(session_id, host))
        return refreshed


class OpenCodeRefreshMemoTests(_OpenCodeBase):
    def test_unrelated_wal_write_does_not_rebuild_untouched_session(self) -> None:
        expected_a = self._uncached("ses_a")
        self.assertParity("ses_a")
        self.assertParity("ses_b")
        real = oc._build_session_info_v2
        self._message("ses_b", 2, "user", 20, {"text": "follow-up"})
        self.writer.commit()
        expected_b = self._uncached("ses_b")
        with mock.patch.object(oc, "_build_session_info_v2", side_effect=real) as build:
            untouched = oc.refresh_session(self._session("ses_a"))
            self.assertEqual(build.call_count, 0)
        with mock.patch.object(oc, "_build_session_info_v2", side_effect=real) as build:
            changed = oc.refresh_session(self._session("ses_b"))
            self.assertEqual(build.call_count, 1)
        self.assertEqual(untouched, expected_a)
        self.assertEqual(changed, expected_b)
        assert untouched is not None and changed is not None
        self.assertEqual(untouched["last_agent_msg"], "reply ses_a")
        self.assertEqual(changed["last_user_msg"], "follow-up")
        self.assertEqual(changed["status_tag"], titles.STATUS_PENDING)

    def test_scan_warmed_memo_is_reused_by_refresh(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_DATA_DIR": str(self.root)}), \
                mock.patch.object(oc, "live_processes", return_value=[]):
            listed = {item["id"]: item for item in oc.scan_sessions(limit=10)}
        expected = {session_id: self._uncached(session_id) for session_id in ("ses_a", "ses_b")}
        real = oc._build_session_info_v2
        with mock.patch.object(oc, "_build_session_info_v2", side_effect=real) as build:
            refreshed = {
                session_id: oc.refresh_session(listed[session_id])
                for session_id in ("ses_a", "ses_b")
            }
        self.assertEqual(build.call_count, 0)
        self.assertEqual(refreshed, expected)

    def test_append_rewrite_delete_and_row_update_match_uncached(self) -> None:
        self.assertParity("ses_a")
        self._message("ses_a", 2, "user", 20, {"text": "new question"})
        self.writer.commit()
        self.assertEqual(self.assertParity("ses_a")["last_user_msg"], "new question")

        self._message(
            "ses_a", 1, "assistant", 11,
            {"content": [{"type": "text", "text": "a longer edited reply"}], "finish": "stop"},
        )
        self.writer.commit()
        self.assertEqual(self.assertParity("ses_a")["last_agent_msg"], "a longer edited reply")

        self.writer.execute("DELETE FROM session_message WHERE id = 'ses_a#2'")
        self.writer.commit()
        self.assertEqual(self.assertParity("ses_a")["status_tag"], titles.STATUS_DONE)

        self.writer.execute(
            "UPDATE session_v2 SET title = ?, time_updated = ? WHERE id = 'ses_a'",
            ("retitled", 300),
        )
        self.writer.commit()
        retitled = self.assertParity("ses_a")
        self.assertEqual(retitled["native_title"], "retitled")
        self.assertEqual(retitled["mtime"], 0.3)

    def test_same_length_rewrite_with_native_timestamp_bump_rebuilds(self) -> None:
        self.assertParity("ses_a")
        self._message(
            "ses_a", 1, "assistant", 12,
            {"content": [{"type": "text", "text": "REPLY SES_A"}], "finish": "stop"},
        )
        self.writer.commit()
        refreshed = self.assertParity("ses_a")
        self.assertEqual(refreshed["last_agent_msg"], "REPLY SES_A")

    def test_version_query_failure_builds_without_memo(self) -> None:
        with mock.patch.object(oc, "_v2_session_versions", return_value=None):
            self.assertParity("ses_a")
        self.assertParity("ses_a")

    def test_host_transform_change_misses_memo(self) -> None:
        upper = HostExtension(name="upper", excerpt_preprocess=str.upper)
        lower = HostExtension(name="lower", excerpt_preprocess=str.lower)
        upper_info = self.assertParity("ses_a", upper)
        lower_info = self.assertParity("ses_a", lower)
        self.assertEqual(upper_info["last_agent_msg"], "REPLY SES_A")
        self.assertEqual(lower_info["last_agent_msg"], "reply ses_a")

    def test_concurrent_refreshes_are_consistent(self) -> None:
        expected = {session_id: self._uncached(session_id) for session_id in ("ses_a", "ses_b")}

        def run(index: int) -> dict | None:
            return oc.refresh_session(self._session("ses_a" if index % 2 else "ses_b"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(64)))
        self.assertEqual(
            results,
            [expected["ses_a"] if index % 2 else expected["ses_b"] for index in range(64)],
        )

    def test_session_row_gone_is_none(self) -> None:
        self.assertIsNone(oc.refresh_session(self._session("ses_gone")))


if __name__ == "__main__":
    unittest.main()
