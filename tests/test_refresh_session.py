"""refresh_session: one listed session re-derived exactly like the next scan."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit import registry, titles
from sesskit.parsers import claude, codex, cursor, kimi, opencode, pi

_LIVENESS = ("live", "pid")


def _listing(info: dict) -> dict:
    return {key: value for key, value in info.items() if key not in _LIVENESS}


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _append_rows(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class _NullCache:
    def get_session(self, *_args, **_kwargs):
        return None

    def put_session(self, *_args, **_kwargs):
        return None


class ClaudeRefreshTests(unittest.TestCase):
    def _user(self, cwd: str, text: str, uuid: str, ts: str) -> dict:
        return {
            "type": "user", "cwd": cwd, "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    def _assistant(self, text: str, stop: str, uuid: str, ts: str) -> dict:
        return {
            "type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {
                "role": "assistant", "stop_reason": stop,
                "content": [{"type": "text", "text": text}],
            },
        }

    def test_matches_scan_and_follows_turn_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            path = project / "a1b2c3d4-0000-4000-8000-000000000001.jsonl"
            _write_rows(path, [
                self._user(tmp, "do the thing", "u-1", "2026-10-06T04:00:00.000Z"),
                self._assistant("looking", "tool_use", "a-1", "2026-10-06T04:00:01.000Z"),
            ])
            with mock.patch.object(claude, "PROJECTS_DIR", str(projects)), \
                    mock.patch.object(claude, "_live_session_ids", return_value={}), \
                    mock.patch.object(claude, "get_cache", return_value=_NullCache()):
                listed = claude.scan_sessions(limit=5)[0]
                running = claude.refresh_session(listed)
                assert running is not None
                self.assertEqual(_listing(running), _listing(listed))
                self.assertNotEqual(running["status_tag"], titles.STATUS_DONE)
                self.assertEqual(running["completion_id"], "")

                _append_rows(path, [
                    self._assistant("all done", "end_turn", "a-2", "2026-10-06T04:00:05.000Z"),
                ])
                finished = claude.refresh_session(listed)
                rescanned = claude.scan_sessions(limit=5)[0]
            assert finished is not None
            self.assertEqual(finished["status_tag"], titles.STATUS_DONE)
            self.assertTrue(finished["completion_id"])
            self.assertEqual(finished["last_agent_msg"], "all done")
            self.assertEqual(_listing(finished), _listing(rescanned))

    def test_missing_history_is_none(self) -> None:
        self.assertIsNone(claude.refresh_session({"source": "claude", "path": "/nonexistent/x.jsonl"}))
        self.assertIsNone(claude.refresh_session({"source": "claude", "path": ""}))


class CodexRefreshTests(unittest.TestCase):
    SID = "01a0a432-a844-7300-951f-c6cc548cb2f4"

    def test_matches_scan_after_native_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-10-06T12-00-00-{self.SID}.jsonl"
            _write_rows(path, [
                {"timestamp": "2026-10-06T04:00:00.000Z", "type": "session_meta",
                 "payload": {"id": self.SID, "cwd": tmp, "thread_source": "user"}},
                {"timestamp": "2026-10-06T04:00:01.000Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "do the thing"}},
            ])
            patches = (
                mock.patch.object(codex, "_find_all_session_files", return_value=[str(path)]),
                mock.patch.object(codex, "_load_index", return_value={}),
                mock.patch.object(codex, "_live_session_ids", return_value={}),
                mock.patch.object(codex, "get_cache", return_value=_NullCache()),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                listed = codex.scan_sessions(limit=5)[0]
                self.assertEqual(_listing(codex.refresh_session(listed)), _listing(listed))
                _append_rows(path, [
                    {"timestamp": "2026-10-06T04:00:09.000Z", "type": "event_msg",
                     "payload": {"type": "task_complete", "turn_id": "turn-1",
                                 "last_agent_message": "PONG"}},
                ])
                finished = codex.refresh_session(listed)
                rescanned = codex.scan_sessions(limit=5)[0]
            assert finished is not None
            self.assertEqual(finished["status_tag"], titles.STATUS_DONE)
            self.assertTrue(finished["completion_id"])
            self.assertEqual(_listing(finished), _listing(rescanned))


class PiRefreshTests(unittest.TestCase):
    def test_matches_builder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-10-06T04-00-00-000Z_pi-session-1.jsonl"
            _write_rows(path, [
                {"type": "session", "id": "pi-session-1", "cwd": tmp,
                 "timestamp": "2026-10-06T04:00:00.000Z"},
                {"type": "message", "timestamp": "2026-10-06T04:00:01.000Z",
                 "message": {"role": "user", "content": "do the thing"}},
                {"type": "message", "timestamp": "2026-10-06T04:00:02.000Z",
                 "message": {"role": "assistant", "content": "done", "stopReason": "stop"}},
            ])
            built, _created = pi._build_session_info(str(path))
            refreshed = pi.refresh_session({"source": "pi", "path": str(path)})
        assert refreshed is not None
        self.assertEqual(refreshed, built)
        self.assertEqual(refreshed["status_tag"], titles.STATUS_DONE)
        self.assertTrue(refreshed["completion_id"])


class CursorRefreshTests(unittest.TestCase):
    CHAT_ID = "11111111-1111-4111-8111-111111111111"

    def test_matches_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            chat = root / "ws" / self.CHAT_ID
            chat.mkdir(parents=True)
            (chat / "meta.json").write_text(json.dumps({
                "createdAtMs": 1790747744885, "updatedAtMs": 1790747754778,
                "hasConversation": True, "cwd": str(root / "proj"), "title": "Chat",
            }), encoding="utf-8")
            (chat / "prompt_history.json").write_text(json.dumps(["hello"]), encoding="utf-8")
            with mock.patch.object(cursor, "CHATS_DIR", str(root)), \
                    mock.patch.object(cursor, "get_cache", return_value=_NullCache()), \
                    mock.patch.object(cursor, "_apply_live_flags"):
                listed = cursor.scan_sessions(limit=5)[0]
                refreshed = cursor.refresh_session(listed)
        assert refreshed is not None
        self.assertEqual(_listing(refreshed), _listing(listed))


class OpenCodeRefreshTests(unittest.TestCase):
    def _db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
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
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO session VALUES ('s1', '/repo', 'Title', 100, 300, NULL, NULL)")
        conn.execute("INSERT INTO session VALUES ('s2', '/repo', 'Other', 100, 200, NULL, NULL)")
        for mid, sid, ts, role, text in (
            ("m1", "s1", 101, "user", "first question"),
            ("m2", "s1", 102, "assistant", "agent answer"),
            ("m3", "s2", 103, "user", "other question"),
        ):
            data = {"role": role}
            if role == "assistant":
                data["time"] = {"completed": ts}
                data["finish"] = "stop"
            conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, ts, ts, json.dumps(data)))
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"p-{mid}", mid, sid, ts, ts, json.dumps({"type": "text", "text": text})),
            )
        conn.commit()
        conn.close()

    def test_matches_scan_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            self._db(db)
            with mock.patch.object(opencode, "_db_paths", return_value=[str(db)]), \
                    mock.patch.object(opencode, "_apply_live_flags"):
                listed = {item["id"]: item for item in opencode.scan_sessions(limit=5)}
            for session_id in ("s1", "s2"):
                with self.subTest(session=session_id):
                    refreshed = opencode.refresh_session(listed[session_id])
                    assert refreshed is not None
                    self.assertEqual(_listing(refreshed), _listing(listed[session_id]))
            self.assertIsNone(opencode.refresh_session({"source": "opencode", "path": str(db), "id": "gone"}))


class KimiRefreshTests(unittest.TestCase):
    def test_matches_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            session_dir = root / "wd_demo" / "session_1"
            wire = session_dir / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            (session_dir / "state.json").write_text(json.dumps({
                "workDir": tmp, "title": "kimi fixture",
                "createdAt": "2026-09-29T00:00:00Z", "updatedAt": "2026-09-29T00:00:01Z",
            }), encoding="utf-8")
            wire.write_text(json.dumps({
                "type": "context.append_message", "time": 1_758_000_000_000,
                "message": {"role": "user", "content": [{"type": "text", "text": "do the thing"}]},
            }) + "\n", encoding="utf-8")
            with mock.patch.object(kimi, "SESSIONS_DIR", str(root)), \
                    mock.patch.object(kimi, "get_cache", return_value=_NullCache()), \
                    mock.patch.object(kimi, "_apply_live_flags"):
                listed = kimi.scan_sessions(limit=5, include_missing_cwd=True)
            self.assertEqual(len(listed), 1)
            refreshed = kimi.refresh_session(listed[0])
        assert refreshed is not None
        self.assertEqual(_listing(refreshed), _listing(listed[0]))


class RegistryRefreshTests(unittest.TestCase):
    def test_dispatches_by_source(self) -> None:
        with mock.patch.object(pi, "refresh_session", return_value={"id": "x"}) as patched:
            self.assertEqual(registry.refresh_session({"source": "pi", "path": "p"}), {"id": "x"})
            self.assertEqual(
                registry.default_registry().get("pi").refresh_session({"path": "p"}), {"id": "x"},
            )
        self.assertEqual(patched.call_count, 2)
        self.assertEqual(patched.call_args.args[0]["source"], "pi")

    def test_unknown_runtime_is_none(self) -> None:
        self.assertIsNone(registry.refresh_session({"source": "nope", "path": "p"}))


if __name__ == "__main__":
    unittest.main()
