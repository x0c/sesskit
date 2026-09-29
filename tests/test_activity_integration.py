"""Integration checks for typed activity dispatch and v1 projection wiring."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.transcript import load_events


class ActivityIntegrationTests(unittest.TestCase):
    def test_cursor_load_events_uses_typed_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            conn.execute(
                "INSERT INTO blobs VALUES (?, ?)",
                ("one", json.dumps({"role": "user", "content": "hello"}).encode()),
            )
            conn.commit()
            conn.close()
            session = {"source": "cursor", "path": str(path), "id": "cursor"}
            self.assertEqual(load_events(session), to_v1_dicts(load_activity(session)))

    def test_opencode_load_events_uses_typed_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE session (id TEXT PRIMARY KEY);
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
                    time_created INTEGER, data TEXT);
                CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
                    session_id TEXT, time_created INTEGER, data TEXT);
                """
            )
            conn.execute("INSERT INTO session VALUES ('s1')")
            conn.execute(
                "INSERT INTO message VALUES ('m1', 's1', 1000, ?)",
                (json.dumps({"role": "user", "time": {"created": 1000}}),),
            )
            conn.execute(
                "INSERT INTO part VALUES ('p1', 'm1', 's1', 1000, ?)",
                (json.dumps({"type": "text", "text": "hello"}),),
            )
            conn.commit()
            conn.close()
            session = {"source": "opencode", "path": str(path), "id": "s1"}
            self.assertEqual(load_events(session), to_v1_dicts(load_activity(session)))

    def test_kimi_remains_on_legacy_typed_unsupported_path(self) -> None:
        self.assertEqual(load_activity({"source": "kimi"}).state, "unsupported")


if __name__ == "__main__":
    unittest.main()
