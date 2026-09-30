"""OpenCode list signature: stable on WAL-only churn, flips on list-relevant writes.

Contract under test (docs/CONTRACT.md, Runtime-specific restore semantics,
OpenCode item): `scan_signature` must not embed `opencode.db-wal` mtime.
It is a read-only content fingerprint per database, hashing all row values
for the tracked session/message tables in stable key order, plus the
live-process snapshot. Busy/locked/missing-table databases fall back to the
old file-mtime behavior without raising.

These tests build synthetic databases (WAL mode on, like production) and
drive the real `scan_signature`; `live_processes` is stubbed so only the
DB signal varies, except the one test that asserts process churn still
flips the signature.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest

from sesskit import titles
from sesskit.parsers import opencode as oc

_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, directory TEXT, title TEXT,
    time_created INTEGER, time_updated INTEGER,
    parent_id TEXT, time_archived INTEGER);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
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
CREATE TABLE todo (session_id TEXT, content TEXT);
"""


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("ses_v1", "/repo/a", "v1 title", 1000, 2000, None, None),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses_v1", 1001, 1001, json.dumps({"role": "user"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p1", "m1", "ses_v1", 1001, 1001,
         json.dumps({"type": "text", "text": "hi"})),
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
        ("ses_v2", "/repo/b", "v2 title", 1000, 2000, None, None),
    )
    conn.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
        ("sm1", "ses_v2", "user", 0, 1001, 1001,
         json.dumps({"time": {"created": 1001}, "text": "hi"})),
    )
    conn.commit()


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "opencode.db")
        self.writer = sqlite3.connect(self.db_path)
        self.addCleanup(self.writer.close)
        self.writer.executescript(_SCHEMA)
        # Production runs WAL mode; the -wal sidecar must exist so the
        # signature cache sees a stable source stamp across read-only opens.
        self.writer.execute("PRAGMA journal_mode=WAL")
        _seed(self.writer)
        self.old_env = os.environ.get("OPENCODE_DATA_DIR")
        os.environ["OPENCODE_DATA_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)
        self.old_live = oc.live_processes
        oc.live_processes = lambda _name: ()
        self.addCleanup(self._restore_live)

    def _restore_env(self):
        if self.old_env is None:
            os.environ.pop("OPENCODE_DATA_DIR", None)
        else:
            os.environ["OPENCODE_DATA_DIR"] = self.old_env

    def _restore_live(self):
        oc.live_processes = self.old_live

    def _write(self, sql: str, args: tuple = ()):
        self.writer.execute(sql, args)
        self.writer.commit()

    def _sig(self):
        sig = oc.scan_signature()
        self.assertIsNotNone(sig)
        return sig

    def _add_v2_session(self, session_id: str, messages: list[tuple]):
        self._write(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
            (session_id, "/repo/signature", session_id, 1, 100, None, None),
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                [
                    (f"{session_id}#{seq}", session_id, kind, seq, created,
                     updated, json.dumps(data))
                    for seq, kind, created, updated, data in messages
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _list_info(self, session_id: str) -> dict:
        sessions = oc.scan_sessions(limit=500)
        info = next((s for s in sessions if s["id"] == session_id), None)
        self.assertIsNotNone(info)
        return info

    # -- stability ----------------------------------------------------

    def test_wal_mtime_churn_and_unrelated_table_writes_do_not_flip(self):
        before = self._sig()
        # Pure filesystem churn: bump both mtimes with no content change.
        # The old signature would flip here; the new one must not.
        now = time.time() + 50
        os.utime(self.db_path, (now, now))
        wal = self.db_path + "-wal"
        if os.path.isfile(wal):
            os.utime(wal, (now, now))
        # Write to a table the list never reads.
        self._write(
            "INSERT INTO todo VALUES (?, ?)", ("ses_v1", "unrelated chore")
        )
        self.assertEqual(self._sig(), before)

    def test_part_only_streaming_growth_does_not_flip(self):
        before = self._sig()
        self._write(
            "UPDATE part SET data = ?, time_updated = ? WHERE id = 'p1'",
            (json.dumps({"type": "text", "text": "hi plus streamed tokens"}),
             9999),
        )
        self.assertEqual(self._sig(), before)

    def test_empty_db_signature_is_stable(self):
        path = os.path.join(self.tmp.name, "empty.db")
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        os.environ["OPENCODE_DATA_DIR"] = self.tmp.name
        # Point at a directory holding only the empty db.
        sub = os.path.join(self.tmp.name, "sub")
        os.mkdir(sub)
        os.rename(path, os.path.join(sub, "opencode.db"))
        os.environ["OPENCODE_DATA_DIR"] = sub
        self.assertEqual(self._sig(), self._sig())

    # -- list-relevant changes flip ------------------------------------

    def test_new_v1_session_flips(self):
        before = self._sig()
        self._write(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            ("ses_new", "/repo/n", "new", 3000, 3000, None, None),
        )
        self.assertNotEqual(self._sig(), before)

    def test_new_v2_session_flips(self):
        before = self._sig()
        self._write(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
            ("ses_new2", "/repo/n2", "new2", 3000, 3000, None, None),
        )
        self.assertNotEqual(self._sig(), before)

    def test_title_change_flips(self):
        before = self._sig()
        self._write(
            "UPDATE session_v2 SET title = ?, time_updated = ? WHERE id = ?",
            ("renamed", 5000, "ses_v2"),
        )
        self.assertNotEqual(self._sig(), before)

    def test_new_v1_message_flips(self):
        before = self._sig()
        self._write(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m2", "ses_v1", 4000, 4000,
             json.dumps({"role": "assistant", "finish": "stop"})),
        )
        self.assertNotEqual(self._sig(), before)

    def test_v1_inplace_error_mark_flips(self):
        """In-place status change (same time_created, error added) flips."""
        before = self._sig()
        self._write(
            "UPDATE message SET data = ?, time_updated = ? WHERE id = 'm1'",
            (json.dumps({"role": "assistant", "error": "boom"}), 6000),
        )
        self.assertNotEqual(self._sig(), before)

    def test_new_v2_message_flips(self):
        before = self._sig()
        self._write(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            ("sm2", "ses_v2", "assistant", 1, 4000, 4000,
             json.dumps({"time": {"created": 4000},
                         "content": [{"type": "text", "text": "done"}],
                         "finish": "stop"})),
        )
        self.assertNotEqual(self._sig(), before)

    def test_higher_seq_v2_message_with_tied_timestamps_flips_and_updates_list(self):
        session_id = "ses_v2_tied"
        self._add_v2_session(
            session_id,
            [(0, "user", 100, 100, {"text": "question"})],
        )
        before = self._sig()
        self.assertEqual(
            self._list_info(session_id)["status_tag"], titles.STATUS_PENDING
        )

        # Same time_created/time_updated as existing maxima, but the higher
        # sequence becomes the visible assistant tail.
        self._write(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"{session_id}#1", session_id, "assistant", 1, 100, 100,
             json.dumps({"content": [{"type": "text", "text": "tied reply"}],
                         "finish": "stop"})),
        )
        self.assertNotEqual(self._sig(), before)
        after = self._list_info(session_id)
        self.assertEqual(after["status_tag"], titles.STATUS_DONE)
        self.assertEqual(after["last_agent_msg"], "tied reply")

    def test_nonmaximum_v2_row_edit_changes_visible_text_and_signature(self):
        session_id = "ses_v2_nonmax_edit"
        self._add_v2_session(
            session_id,
            [
                (0, "user", 100, 100, {"text": "question"}),
                (1, "assistant", 10, 10,
                 {"content": [{"type": "text", "text": "old reply"}],
                  "finish": "stop"}),
                (2, "user", 100, 100, {"text": "follow-up"}),
            ],
        )
        before = self._sig()
        self.assertEqual(self._list_info(session_id)["last_agent_msg"], "old reply")
        self._write(
            "UPDATE session_message SET data = ? WHERE id = ?",
            (json.dumps({"content": [{"type": "text", "text": "edited reply"}],
                         "finish": "stop"}), f"{session_id}#1"),
        )
        self.assertNotEqual(self._sig(), before)
        self.assertEqual(self._list_info(session_id)["last_agent_msg"], "edited reply")

    def test_nonmaximum_v2_row_deletion_changes_tail_status_and_signature(self):
        session_id = "ses_v2_nonmax_delete"
        self._add_v2_session(
            session_id,
            [
                (0, "user", 100, 100, {"text": "question"}),
                (1, "user", 100, 100, {"text": "last user"}),
                (2, "assistant", 10, 10,
                 {"content": [{"type": "text", "text": "older done"}],
                  "finish": "stop"}),
            ],
        )
        before = self._sig()
        self.assertEqual(
            self._list_info(session_id)["status_tag"], titles.STATUS_DONE
        )
        self._write(
            "DELETE FROM session_message WHERE id = ?", (f"{session_id}#2",)
        )
        self.assertNotEqual(self._sig(), before)
        after = self._list_info(session_id)
        self.assertEqual(after["status_tag"], titles.STATUS_PENDING)
        self.assertEqual(after["last_agent_msg"], "")

    def test_v2_inplace_update_flips(self):
        before = self._sig()
        self._write(
            "UPDATE session_message SET time_updated = ? WHERE id = 'sm1'",
            (7000,),
        )
        self.assertNotEqual(self._sig(), before)

    # -- process snapshot still participates ----------------------------

    def test_live_process_churn_flips(self):
        oc.live_processes = lambda _name: ((1234, "/repo"),)
        before = self._sig()
        oc.live_processes = lambda _name: ((1234, "/repo"), (5678, "/x"))
        self.assertNotEqual(self._sig(), before)
        oc.live_processes = lambda _name: ()

    # -- fallback and safety ---------------------------------------------

    def test_missing_v2_tables_falls_back_to_file_mtime(self):
        sub = os.path.join(self.tmp.name, "old")
        os.mkdir(sub)
        old_db = os.path.join(sub, "opencode.db")
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, time_updated INTEGER)"
        )
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, time_created INTEGER,"
            " time_updated INTEGER)"
        )
        conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        os.environ["OPENCODE_DATA_DIR"] = sub
        sig = self._sig()
        # Fallback shape carries the file list, never raises.
        self.assertIn("files", repr(sig))
        now = time.time() + 60
        os.utime(old_db, (now, now))
        self.assertNotEqual(self._sig(), sig)

    def test_locked_db_falls_back_without_raising(self):
        old_connect = oc.connect_ro

        def _busy(_path: str):
            raise sqlite3.OperationalError("database is locked")

        oc.connect_ro = _busy  # type: ignore[assignment]
        try:
            sig = self._sig()
        finally:
            oc.connect_ro = old_connect
        self.assertIn("files", repr(sig))

    def test_signature_never_writes(self):
        # Read-only guarantee is about DB *content*: bytes identical, no
        # rollback-journal (a WAL -shm/-wal sidecar may appear — SQLite
        # creates those for any WAL read, including the pre-existing
        # scan_sessions path; the -wal stays 0 bytes and content is
        # untouched).
        with open(self.db_path, "rb") as fh:
            before_bytes = fh.read()
        for _ in range(3):
            self._sig()
        with open(self.db_path, "rb") as fh:
            self.assertEqual(fh.read(), before_bytes)
        leftovers = [f for f in os.listdir(self.tmp.name)
                     if f.endswith("-journal")]
        self.assertEqual(leftovers, [])
        wal = self.db_path + "-wal"
        if os.path.isfile(wal):
            conn = sqlite3.connect(self.db_path)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM session").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 1)

    def test_unchanged_source_reuses_fingerprint_without_reopening_database(self):
        oc._SIGNATURE_FINGERPRINT_CACHE.pop(self.db_path, None)
        real_connect = oc.connect_ro
        open_count = 0

        def counting_connect(path: str):
            nonlocal open_count
            open_count += 1
            return real_connect(path)

        oc.connect_ro = counting_connect
        try:
            first = self._sig()
            second = self._sig()
        finally:
            oc.connect_ro = real_connect
        self.assertEqual(first, second)
        self.assertEqual(open_count, 1)

    def test_source_change_during_hash_is_not_cached(self):
        self._sig()  # Seed an entry, then make a tracked change to force a miss.
        self._write(
            "UPDATE session_v2 SET title = ? WHERE id = ?",
            ("before concurrent edit", "ses_v2"),
        )
        real_fingerprint = oc._content_fingerprint

        def mutate_after_hash(path: str):
            fingerprint = real_fingerprint(path)
            self._write(
                "UPDATE session_v2 SET title = ? WHERE id = ?",
                ("changed during fingerprint", "ses_v2"),
            )
            return fingerprint

        oc._content_fingerprint = mutate_after_hash
        try:
            unstable_signature = self._sig()
        finally:
            oc._content_fingerprint = real_fingerprint

        self.assertNotIn(self.db_path, oc._SIGNATURE_FINGERPRINT_CACHE)
        self.assertIn("files", repr(unstable_signature))
        stable_signature = self._sig()
        self.assertNotEqual(stable_signature, unstable_signature)
        self.assertIn(self.db_path, oc._SIGNATURE_FINGERPRINT_CACHE)


if __name__ == "__main__":
    unittest.main()
