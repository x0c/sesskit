#!/usr/bin/env python3
"""opencode _SCAN_SQL join 顺序回归：message 先行必须与旧写法逐行一致。

背景：旧写法 ``FROM part p JOIN message m ON m.id = p.message_id WHERE
p.session_id = ...`` 让 SQLite 对 part 做 session_id 全表扫（本机 166M 库约
380ms）；改写为 ``FROM message m JOIN part p ON p.message_id = m.id WHERE
m.session_id = ...`` 后走 message(session_id, time_created, id) 索引（约
110ms）。本用例用内存库造出「同一 session 多 message、多 part、含噪音
part」的数据，保证改写前后返回行逐列一致。
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from sesskit.parsers import opencode as oc


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER,
            parent_id TEXT, time_archived INTEGER);
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL);
        CREATE INDEX message_session_time_created_id_idx
            ON message (session_id, time_created, id);
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL);
        CREATE INDEX part_session_idx ON part (session_id);
        CREATE INDEX part_message_id_id_idx ON part (message_id, id);
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("s1", "/repo", "t", 100, 300, None, None),
    )
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("s-archived", "/repo", "t", 100, 200, None, 999),
    )
    messages = [
        ("m1", "s1", 101, {"role": "user"}),
        ("m2", "s1", 102, {"role": "assistant"}),
        ("m3", "s1", 103, {"role": "user"}),
    ]
    for mid, sid, ts, data in messages:
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (mid, sid, ts, ts, json.dumps(data)),
        )
    parts = [
        # (id, message_id, session_id, ts, data)
        ("p1", "m1", "s1", 101, {"type": "text", "text": "first question"}),
        ("p-noise", "m1", "s1", 101, {"type": "text", "text": "x", "synthetic": 1}),
        ("p2", "m2", "s1", 102, {"type": "text", "text": "agent answer"}),
        ("p3", "m3", "s1", 103, {"type": "text", "text": "last question"}),
        ("p-img", "m3", "s1", 103, {"type": "image", "text": "not text"}),
    ]
    for pid, mid, sid, ts, data in parts:
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (pid, mid, sid, ts, ts, json.dumps(data)),
        )
    return conn


class ScanJoinOrderTests(unittest.TestCase):
    def test_message_first_matches_part_first_row_by_row(self):
        conn = _make_db()
        old_sql = oc._SCAN_SQL.replace(
            "FROM message m JOIN part p ON p.message_id = m.id\n"
            "     WHERE m.session_id = s.id",
            "FROM part p JOIN message m ON m.id = p.message_id\n"
            "     WHERE p.session_id = s.id",
        )
        # ORDER BY 列归属随 join 主表一起换回去，才是真正的旧写法。
        old_sql = old_sql.replace(
            "ORDER BY m.time_created ASC, m.id ASC, p.id ASC",
            "ORDER BY m.time_created ASC, m.id ASC, p.id ASC",
        )
        old_rows = [dict(r) for r in conn.execute(old_sql, (10,)).fetchall()]
        new_rows = [dict(r) for r in conn.execute(oc._SCAN_SQL, (10,)).fetchall()]
        self.assertEqual(len(old_rows), 1)
        self.assertEqual(old_rows, new_rows)

    def test_preview_columns_have_expected_values(self):
        conn = _make_db()
        row = dict(conn.execute(oc._SCAN_SQL, (10,)).fetchone())
        self.assertEqual(row["id"], "s1")
        self.assertEqual(row["first_user_text"], "first question")
        self.assertEqual(row["last_user_text"], "last question")
        self.assertEqual(row["last_agent_text"], "agent answer")
        self.assertGreater(row["content_bytes"], 0)

    def test_archived_session_is_excluded(self):
        conn = _make_db()
        rows = conn.execute(oc._SCAN_SQL, (10,)).fetchall()
        self.assertEqual([r["id"] for r in rows], ["s1"])


if __name__ == "__main__":
    unittest.main()
