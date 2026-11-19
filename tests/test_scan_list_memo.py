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
from sesskit.cache import file_signature
from sesskit.parsers import antigravity as ag
from sesskit.parsers import codex as scan_codex
from sesskit.parsers import cursor as scan_cursor
from sesskit.parsers import opencode as oc
from sesskit.parsers import pi as scan_pi
from sesskit.parsers.common import MEMO_MISS, HostExtension, ListRecordMemo


class _MemorySessionCache:
    """In-memory test double implementing both file and versioned L2 cache slots."""

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str], tuple[tuple | None, str, dict]] = {}
        self.versioned: dict[tuple[str, str], tuple[str, str | None, dict]] = {}

    def get_session(self, runtime: str, path: str, extra_version: str = "") -> dict | None:
        entry = self.sessions.get((runtime, path))
        sig = file_signature(path)
        if entry is None or sig is None or entry[0] != sig or entry[1] != extra_version:
            return None
        return dict(entry[2])

    def put_session(self, runtime: str, path: str, payload: dict, extra_version: str = "") -> None:
        sig = file_signature(path)
        if sig is not None:
            self.sessions[(runtime, path)] = (sig, extra_version, dict(payload))

    def get_versioned_session(
        self, runtime: str, key: str, version: str, *, source_path: str | None = None,
    ) -> dict | None:
        entry = self.versioned.get((runtime, key))
        if entry is None or entry[0] != version or entry[1] != source_path:
            return None
        return dict(entry[2])

    def put_versioned_session(
        self, runtime: str, key: str, payload: dict, version: str, *, source_path: str | None = None,
    ) -> None:
        self.versioned[(runtime, key)] = (version, source_path, dict(payload))


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

    def test_v2_persists_to_versioned_l2_and_skips_projection_on_cold_memo(self) -> None:
        cache = _MemorySessionCache()
        host = HostExtension(name="fake", cache=cache)
        oc._V2_LIST_MEMO.clear()
        self.addCleanup(oc._V2_LIST_MEMO.clear)
        first = {s["id"]: s for s in oc.scan_sessions(limit=10, host=host)}
        self.assertEqual(set(first), {"ses_a", "ses_b"})
        self.assertEqual(len(cache.versioned), 2)

        oc._V2_LIST_MEMO.clear()
        with (
            mock.patch.object(
                oc.native_projection, "project_opencode_sessions",
                side_effect=AssertionError("L2 hit must skip Rust projection"),
            ),
            mock.patch.object(
                oc, "_build_session_info_v2",
                side_effect=AssertionError("L2 hit must skip Python v2 builder"),
            ),
        ):
            second = {s["id"]: s for s in oc.scan_sessions(limit=10, host=host)}
        self.assertEqual(second, first)

    def test_v1_cutoff_pruning_skips_older_v1_and_keeps_newer_v1(self) -> None:
        oc._V2_LIST_MEMO.clear()
        self.addCleanup(oc._V2_LIST_MEMO.clear)
        # Insert 1 old v1 session (updated=50 < cutoff 100 when limit=2)
        self.writer.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            ("ses_v1_old", self.cwd, "old v1", 1, 50, None, None),
        )
        self.writer.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m_old", "ses_v1_old", 10, 50, json.dumps({"role": "user"})),
        )
        self.writer.commit()
        with mock.patch.object(oc, "_build_session_info") as v1_build:
            res = oc.scan_sessions(limit=2)
        self.assertEqual([r["id"] for r in res], ["ses_a", "ses_b"])
        self.assertEqual(v1_build.call_count, 0)

        # Insert 1 newer v1 session (updated=500 > cutoff 100): must NOT skip v1
        self.writer.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            ("ses_v1_new", self.cwd, "new v1", 1, 500, None, None),
        )
        self.writer.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m_new", "ses_v1_new", 10, 500, json.dumps({"role": "user"})),
        )
        self.writer.commit()
        res_with_new_v1 = oc.scan_sessions(limit=2)
        self.assertEqual([r["id"] for r in res_with_new_v1], ["ses_v1_new", "ses_a"])


class PiL2AndLazyLsofTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cwd = str(self.root / "proj")
        os.makedirs(self.cwd)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        scan_pi._LIST_MEMO.clear()
        self.addCleanup(scan_pi._LIST_MEMO.clear)

    def test_pi_l2_persists_across_memo_clear_and_skips_lsof_when_claimed(self) -> None:
        path = self.sessions / "2026-09-29T00-00-00-000Z_pi-l2.jsonl"
        path.write_text(
            "".join(json.dumps(e) + "\n" for e in _pi_entries("pi-l2", self.cwd, ["hello pi"])),
            encoding="utf-8",
        )
        cache = _MemorySessionCache()
        real_cwd = os.path.realpath(self.cwd)
        host = HostExtension(
            name="corral",
            pi_instance_env_key="CORRAL_PI_INSTANCE_ID",
            pi_claims_provider=lambda: [{
                "pid": 9001,
                "instance": "inst-1",
                "sessionId": "pi-l2",
                "sequence": 1,
            }],
        )
        with (
            mock.patch.object(scan_pi, "SESSIONS_DIR", str(self.sessions)),
            mock.patch.object(scan_pi, "live_processes", return_value=[(9001, real_cwd)]),
            mock.patch.object(scan_pi, "process_command_line", return_value="pi --approve"),
            mock.patch.object(scan_pi, "process_start_times", return_value={9001: 1_700_000_000.0}),
            mock.patch.object(
                scan_pi, "process_environs",
                return_value={9001: {"CORRAL_PI_INSTANCE_ID": "inst-1"}},
            ),
            mock.patch.object(
                scan_pi, "open_file_paths",
                side_effect=AssertionError("claimed Pi processes must skip lsof"),
            ),
        ):
            host_cached = HostExtension(
                name="corral",
                pi_instance_env_key="CORRAL_PI_INSTANCE_ID",
                pi_claims_provider=host.pi_claims_provider,
                cache=cache,
            )
            scan_pi.reset_live_session_overrides()
            self.addCleanup(scan_pi.reset_live_session_overrides)
            first = scan_pi.scan_sessions(limit=10, host=host_cached)
            self.assertEqual(len(first), 1)
            self.assertTrue(first[0]["live"])
            self.assertEqual(first[0]["pid"], 9001)
            self.assertEqual(len(cache.sessions), 1)

            scan_pi._LIST_MEMO.clear()
            with mock.patch.object(
                scan_pi, "_build_session_info",
                side_effect=AssertionError("L2 cache hit must skip Pi parse"),
            ):
                second = scan_pi.scan_sessions(limit=10, host=host_cached)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["id"], "pi-l2")
            self.assertTrue(second[0]["live"])


class AntigravityCacheTests(unittest.TestCase):
    def test_antigravity_l1_l2_and_refresh_invalidate_cleanly(self) -> None:
        ag._LIST_MEMO.clear()
        with ag._SHARED_CACHE_LOCK:
            ag._SUMMARIES_CACHE = None
            ag._HISTORY_CACHE = None
        self.addCleanup(ag._LIST_MEMO.clear)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".gemini" / "antigravity-cli"
            proj = Path(td) / "proj"
            proj.mkdir()
            logs = root / "brain" / "ag-1" / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            tpath = logs / "transcript.jsonl"
            tpath.write_text(
                "\n".join([
                    json.dumps({
                        "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "status": "DONE", "created_at": "2026-11-10T10:00:00Z",
                        "content": "<USER_REQUEST>initial ask</USER_REQUEST>",
                    }),
                    json.dumps({
                        "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                        "status": "DONE", "created_at": "2026-11-10T10:00:02Z",
                        "content": "initial reply",
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            db_path = root / "conversation_summaries.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER, "
                "last_modified_time TEXT, workspace_uris TEXT, status TEXT, killed INTEGER, "
                "last_user_input_time TEXT, app_data_dir TEXT, "
                "parent_conversation_id TEXT, nesting_depth INTEGER, not_fully_idle INTEGER)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ag-1", "", "", 2, "", json.dumps([f"file://{proj}"]), "CASCADE_RUN_STATUS_IDLE", 0, "", "", "", 0, 0),
            )
            conn.commit()
            conn.close()

            cache = _MemorySessionCache()
            host = HostExtension(name="fake", cache=cache)
            with mock.patch.object(ag, "_roots", return_value=[str(root)]):
                real = ag._build_session_info
                with mock.patch.object(ag, "_build_session_info", side_effect=real) as build:
                    first = ag.scan_sessions(limit=10, host=host)
                    self.assertEqual(len(first), 1)
                    self.assertEqual(build.call_count, 1)
                    ag.scan_sessions(limit=10, host=host)
                    self.assertEqual(build.call_count, 1)

                ag._LIST_MEMO.clear()
                with mock.patch.object(
                    ag, "_build_session_info",
                    side_effect=AssertionError("L2 hit must skip Antigravity parse"),
                ), mock.patch.object(
                    ag, "_transcript_candidates",
                    side_effect=AssertionError("direct refresh must skip dir walk"),
                ):
                    refreshed = ag.refresh_session(first[0], host=host)
                self.assertEqual(refreshed, first[0])

                # Annotation change invalidates cache cleanly
                ann_dir = root / "annotations"
                ann_dir.mkdir(parents=True)
                (ann_dir / "ag-1.pbtxt").write_text('title: "Annotated Title"\n', encoding="utf-8")
                updated = ag.refresh_session(first[0], host=host)
                assert updated is not None
                self.assertEqual(updated["native_title"], "Annotated Title")


class CodexAndCursorFilteredCacheTests(unittest.TestCase):
    def test_codex_caches_subagents_and_empty_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = root / "proj"
            cwd.mkdir()
            (root / "2026" / "11" / "19").mkdir(parents=True)
            empty_rollout = root / "2026" / "11" / "19" / "rollout-2026-11-19T10-00-00-00000000-0000-0000-0000-000000000001.jsonl"
            empty_rollout.write_text('{"type": "turn_context", "payload": {}}\n', encoding="utf-8")
            sub_rollout = root / "2026" / "11" / "19" / "rollout-2026-11-19T10-00-01-00000000-0000-0000-0000-000000000002.jsonl"
            sub_rollout.write_text(
                json.dumps({
                    "timestamp": "2026-11-19T02:00:00Z", "type": "session_meta",
                    "payload": {"id": "00000000-0000-0000-0000-000000000002", "cwd": str(cwd), "thread_source": "subagent"},
                }) + "\n" + json.dumps({
                    "timestamp": "2026-11-19T02:00:01Z", "type": "event_msg",
                    "payload": {"type": "user_message", "message": "subagent ask"},
                }) + "\n",
                encoding="utf-8",
            )
            cache = _MemorySessionCache()
            host = HostExtension(name="fake", cache=cache)
            with (
                mock.patch.object(scan_codex, "SESSIONS_DIR", str(root)),
                mock.patch.object(scan_codex, "SESSION_INDEX", str(root / "missing-index.jsonl")),
                mock.patch.object(scan_codex, "_live_session_ids", return_value={}),
                mock.patch.object(scan_codex, "live_pid_snapshot", return_value=()),
            ):
                real = scan_codex._build_session_info
                with mock.patch.object(scan_codex, "_build_session_info", side_effect=real) as build:
                    self.assertEqual(scan_codex.scan_sessions(limit=10, host=host), [])
                    self.assertEqual(build.call_count, 2)
                    build.reset_mock()
                    self.assertEqual(scan_codex.scan_sessions(limit=10, host=host), [])
                    self.assertEqual(build.call_count, 0)

    def test_cursor_caches_filtered_noise_and_missing_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chats = Path(td) / "chats"
            chat_dir = chats / "ws1" / "chat-noise"
            chat_dir.mkdir(parents=True)
            (chat_dir / "meta.json").write_text(
                json.dumps({"name": "Noise", "cwd": str(Path(td) / "deleted-proj"), "createdAtMs": 1000}),
                encoding="utf-8",
            )
            (chat_dir / "prompt_history.json").write_text(json.dumps(["hello"]), encoding="utf-8")
            cache = _MemorySessionCache()
            host = HostExtension(name="fake", cache=cache)
            with (
                mock.patch.object(scan_cursor, "CHATS_DIR", str(chats)),
                mock.patch.object(scan_cursor, "live_processes", return_value=[]),
            ):
                real = scan_cursor._build_session_info
                with mock.patch.object(scan_cursor, "_build_session_info", side_effect=real) as build:
                    self.assertEqual(scan_cursor.scan_sessions(limit=10, host=host), [])
                    self.assertEqual(build.call_count, 1)
                    build.reset_mock()
                    self.assertEqual(scan_cursor.scan_sessions(limit=10, host=host), [])
                    self.assertEqual(build.call_count, 0)


if __name__ == "__main__":
    unittest.main()
