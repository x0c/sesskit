"""completion_id: per-round stable identity for completion notifications."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit import titles
from sesskit.models import completion_id_for
from sesskit.parsers import codex, pi


def _codex_session(path: Path, sid: str, last_line: dict) -> Path:
    lines = [
        {
            "timestamp": "2026-09-15T08:33:03.000Z",
            "type": "session_meta",
            "payload": {"cwd": "/tmp/demo", "thread_source": "user"},
        },
        {
            "timestamp": "2026-09-15T08:33:04.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do the thing"},
        },
        last_line,
    ]
    path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
    return path


class CompletionIdHelperTests(unittest.TestCase):
    def test_non_terminal_has_empty_id(self) -> None:
        self.assertEqual(
            completion_id_for(status_tag=titles.STATUS_PENDING),
            "",
        )
        self.assertEqual(
            completion_id_for(status_tag=titles.STATUS_NONE),
            "",
        )

    def test_same_round_is_stable(self) -> None:
        first = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-1",
            tail_text="done:hello",
        )
        second = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-1",
            tail_text="done:hello",
        )
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_new_round_changes_id(self) -> None:
        first = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-1",
            tail_text="done:hello",
        )
        second = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-2",
            tail_text="done:hello",
        )
        self.assertNotEqual(first, second)

    def test_metadata_fields_are_not_identity(self) -> None:
        """mtime/size/touch must not appear in the id: same anchor+text is stable."""
        first = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-1",
            tail_text="done:hello",
        )
        # No file_mtime/size_bytes parameters exist anymore; identical
        # anchor+text across rescans, restarts, and metadata touches is one id.
        second = completion_id_for(
            status_tag=titles.STATUS_DONE,
            anchor="uuid-1",
            tail_text="done:hello",
        )
        self.assertEqual(first, second)
        status, digest = first.split(":")
        self.assertEqual(status, titles.STATUS_DONE)
        self.assertEqual(len(digest), 16)

    def test_empty_anchor_has_empty_id(self) -> None:
        """No exact native final anchor: unknown, never notifiable (D4)."""
        self.assertEqual(
            completion_id_for(status_tag=titles.STATUS_DONE, anchor="", tail_text="same text"),
            "",
        )
        self.assertEqual(
            completion_id_for(status_tag=titles.STATUS_ABORTED, anchor=""),
            "",
        )


class CodexCompletionIdTests(unittest.TestCase):
    def test_done_and_error_have_distinct_ids(self) -> None:
        sid = "01a0a432-a844-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            done_path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-32-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "PONG",
                    },
                },
            )
            done = codex._build_session_info(str(done_path), {})
            assert done is not None
            self.assertEqual(done["status_tag"], titles.STATUS_DONE)
            self.assertTrue(done.get("completion_id"))

            err_path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-33-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": None,
                        "error": {"message": "limit hit"},
                    },
                },
            )
            err = codex._build_session_info(str(err_path), {})
            assert err is not None
            self.assertEqual(err["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(err.get("completion_id"))
            self.assertNotEqual(done["completion_id"], err["completion_id"])

    def test_rescan_same_file_is_stable(self) -> None:
        sid = "01a0a432-a844-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-32-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "PONG",
                    },
                },
            )
            first = codex._build_session_info(str(path), {})
            second = codex._build_session_info(str(path), {})
            assert first is not None and second is not None
            self.assertEqual(first["completion_id"], second["completion_id"])

    def test_same_text_different_native_ids_have_distinct_ids(self) -> None:
        """Two genuine turns with identical text: native ids keep them distinct (D3).

        Weak assistant-text-only inference never mints identity, so this uses
        genuine `task_complete` terminal markers with distinct native event ids.
        """
        with tempfile.TemporaryDirectory() as tmp:
            infos = []
            for index, (sid, evt_id) in enumerate(
                [
                    ("01a0a432-a844-7300-951f-c6cc548cb301", "evt-aaa"),
                    ("01a0a432-a844-7300-951f-c6cc548cb302", "evt-bbb"),
                ]
            ):
                path = _codex_session(
                    Path(tmp) / f"rollout-2026-09-15T16-32-5{index}-{sid}.jsonl",
                    sid,
                    {
                        "timestamp": "2026-09-15T08:33:06.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "id": evt_id,
                            "last_agent_message": "all done",
                        },
                    },
                )
                info = codex._build_session_info(str(path), {})
                assert info is not None
                self.assertEqual(info["status_tag"], titles.STATUS_DONE)
                self.assertTrue(info.get("completion_id"))
                infos.append(info)
            self.assertNotEqual(infos[0]["completion_id"], infos[1]["completion_id"])


class CodexModernFinalityTests(unittest.TestCase):
    """Modern Codex turn/item framing: per-item activity is never turn end.

    Post-install rejection (2026-10-01): a live turn streams `response_item`
    assistant messages and `item_completed` AgentMessage/CommandExecution
    records (phase often missing; one earlier final-answer item followed by
    more activity). The official SDK collects items and returns only after the
    matching turn-completed notification; final-answer text is response
    selection, not turn end. Treating any trailing assistant row as DONE mints
    a fresh completion id per streamed item (~60 s repeat pushes).
    Rule: with modern turn framing in evidence, assistant rows are activity;
    terminal states come only from `task_complete` / `turn_aborted`.
    Legacy files without modern markers keep exact old behavior.
    """

    SID = "01a0f3e5-b4a0-7912-ba12-f37d06b39289"

    def _write(self, tmp: str, name: str, rows: list[dict]) -> str:
        path = str(Path(tmp) / name)
        Path(path).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return path

    def _path(self, tmp: str) -> str:
        return str(Path(tmp) / f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl")

    def _meta(self) -> dict:
        return {
            "timestamp": "2026-10-01T03:58:32.000Z",
            "type": "session_meta",
            "payload": {"cwd": "/tmp/demo", "thread_source": "user"},
        }

    def _task_started(self) -> dict:
        return {
            "timestamp": "2026-10-01T03:58:33.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        }

    def _user_item(self, uid: str, text: str, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "id": uid, "content": text},
                "turn_id": "turn-1",
            },
        }

    def _agent_response(self, mid: str, text: str, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": mid,
                "content": [{"type": "output_text", "text": text}],
            },
        }

    def _agent_item(self, mid: str, text: str, ts: str, phase: str | None = None) -> dict:
        item: dict = {"type": "AgentMessage", "id": mid, "content": text}
        if phase is not None:
            item["phase"] = phase
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": item, "turn_id": "turn-1"},
        }

    def _cmd_item(self, cid: str, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": cid, "status": "completed"},
                "turn_id": "turn-1",
            },
        }

    def _task_complete(self, ts: str, msg: str = "PONG") -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "task_complete", "id": "evt-1", "last_agent_message": msg},
        }

    def _parse(self, path: str):
        info = codex._build_session_info(path, {})
        assert info is not None
        return info

    def test_midturn_assistant_items_are_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._agent_response("msg-a1", "working on it", "2026-10-01T03:58:40.000Z"),
                self._cmd_item("c-1", "2026-10-01T03:59:00.000Z"),
                self._agent_response("msg-a2", "still going", "2026-10-01T04:03:41.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_final_answer_followed_by_activity_is_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._agent_response("msg-a1", "working", "2026-10-01T03:58:40.000Z"),
                self._agent_item("a-2", "all done", "2026-10-01T03:59:30.000Z",
                                 phase="final_answer"),
                self._agent_item("a-3", "hmm, more", "2026-10-01T04:03:31.000Z"),
                self._cmd_item("c-2", "2026-10-01T04:03:40.000Z"),
                self._agent_response("msg-a3", "still going", "2026-10-01T04:03:41.000Z"),
                self._cmd_item("c-3", "2026-10-01T04:03:50.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_phase_none_commentary_never_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._agent_item("a-1", "note one", "2026-10-01T03:58:40.000Z"),
                self._cmd_item("c-1", "2026-10-01T03:59:00.000Z"),
                self._agent_item("a-2", "note two", "2026-10-01T04:03:31.000Z"),
                self._cmd_item("c-2", "2026-10-01T04:03:40.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_modern_task_complete_still_done_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            name = f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl"
            path = self._write(tmp, name, [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._agent_response("msg-a1", "working", "2026-10-01T03:58:40.000Z"),
                self._task_complete("2026-10-01T04:04:00.000Z"),
            ])
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.writelines(
                    json.dumps({"timestamp": "2026-10-01T04:04:01.000Z",
                                "type": "event_msg",
                                "payload": {"type": "token_count", "total": 7}}) + "\n"
                    for _ in range(20)
                )
            second = self._parse(path)
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertEqual(first["completion_id"], second["completion_id"])

    def test_modern_turn_aborted_still_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._agent_response("msg-a1", "working", "2026-10-01T03:58:40.000Z"),
                {
                    "timestamp": "2026-10-01T04:04:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "id": "evt-9"},
                },
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(info.get("completion_id"))

    def test_legacy_trailing_assistant_display_done_empty_id(self) -> None:
        """Weak legacy inference: display stays DONE with excerpts, but the
        notification identity is empty without native turn-end evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                {
                    "timestamp": "2026-09-15T08:33:03.000Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/demo", "thread_source": "user"},
                },
                {
                    "timestamp": "2026-09-15T08:33:04.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "do the thing"},
                },
                {
                    "timestamp": "2026-09-15T08:33:05.000Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "PONG"},
                },
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info.get("completion_id"), "")
            self.assertEqual(info["last_user_msg"], "do the thing")
            self.assertEqual(info["last_agent_msg"], "PONG")

    def test_evicted_framing_trailing_text_has_empty_id(self) -> None:
        """Framing-eviction shape (42 KB rejection): modern markers outside
        both bounded windows, trailing assistant progress text. Display stays
        DONE for compatibility, but no notification identity may exist."""
        with tempfile.TemporaryDirectory() as tmp:
            rows: list[dict] = [
                {"type": "session_meta",
                 "payload": {"cwd": "/tmp", "thread_source": "user"}},
                {"timestamp": "2026-10-01T04:05:00Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "synthetic task"}},
            ]
            for index in range(130):
                rows.append(
                    {"timestamp": "2026-10-01T04:05:00Z", "type": "event_msg",
                     "payload": {"type": "token_count",
                                 "padding": "metadata" * 8}})
            rows.append(
                {"timestamp": "2026-10-01T04:05:01Z", "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "t1"}})
            rows.append(
                {"timestamp": "2026-10-01T04:05:02Z", "type": "event_msg",
                 "payload": {
                     "type": "item_completed",
                     "item": {"type": "CommandExecution", "id": "c-1",
                              "status": "completed"},
                     "turn_id": "t1"}})
            for index in range(12):
                rows.append(
                    {"timestamp": "2026-10-01T04:05:03Z", "type": "event_msg",
                     "payload": {"type": "token_count",
                                 "padding": "metadata" * 100}})
            rows.append(
                {"timestamp": "2026-10-01T04:05:04Z", "type": "response_item",
                 "payload": {
                     "type": "message",
                     "role": "assistant",
                     "id": "msg-tail",
                     "content": [{"type": "output_text",
                                  "text": "trailing progress"}]}})
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", rows)
            self.assertGreater(Path(path).stat().st_size, 8192)
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info.get("completion_id"), "")
            self.assertEqual(info["last_agent_msg"], "trailing progress")


    def test_new_turn_start_without_messages_is_not_terminal(self) -> None:
        """An earlier completion followed by a bare new turn start: unknown."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_prior_terminal_invalidated_by_new_turn_command(self) -> None:
        """REJECTED-2 case 1: earlier task_complete + new turn + command item."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
                self._cmd_item("c-9", "2026-10-01T04:00:10.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_prior_terminal_invalidated_by_new_turn_agent_item(self) -> None:
        """REJECTED-2 case 2: earlier task_complete + new turn + message item."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
                self._agent_item("a-9", "still working", "2026-10-01T04:00:10.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_prior_abort_invalidated_by_new_turn_command(self) -> None:
        """REJECTED-2 case 3: earlier turn_aborted + new turn + command item."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                {
                    "timestamp": "2026-10-01T03:59:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "id": "end-abort"},
                },
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
                self._cmd_item("c-9", "2026-10-01T04:00:10.000Z"),
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_later_terminal_closes_new_turn_with_new_id(self) -> None:
        """A genuine later terminal marker ends the new turn with a fresh id."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
                self._cmd_item("c-9", "2026-10-01T04:00:10.000Z"),
            ])
            mid = self._parse(path)
            self.assertEqual(mid["status_tag"], titles.STATUS_NONE)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    self._task_complete("2026-10-01T04:01:00.000Z")) + "\n")
            # New terminal record needs its own native id to mint a new round.
            raw = Path(path).read_text(encoding="utf-8").splitlines()
            last = json.loads(raw[-1])
            last["payload"]["id"] = "end-two"
            raw[-1] = json.dumps(last)
            Path(path).write_text("\n".join(raw) + "\n", encoding="utf-8")
            done = self._parse(path)
            self.assertEqual(done["status_tag"], titles.STATUS_DONE)
            self.assertTrue(done.get("completion_id"))

    def test_token_metadata_preserves_completed_identity(self) -> None:
        """Token counts/metadata after task_complete must not churn the id."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
            ])
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            with open(path, "a", encoding="utf-8") as handle:
                handle.writelines(
                    json.dumps({"timestamp": "2026-10-01T04:00:00.000Z",
                                "type": "event_msg",
                                "payload": {"type": "token_count", "total": 7}}) + "\n"
                    for _ in range(20)
                )
            second = self._parse(path)
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertEqual(first["completion_id"], second["completion_id"])

    def test_evicted_terminal_with_newer_activity_is_not_terminal(self) -> None:
        """Bounded eviction: old task_complete pushed out of the 8 KB tail by
        newer item activity leaves no terminal evidence in-window: unknown."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                self._meta(),
                self._task_started(),
                self._user_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
            ]
            for index in range(60):
                rows.append(self._cmd_item(
                    f"c-evict-{index}",
                    f"2026-10-01T04:{10 + index // 60:02d}:{index % 60:02d}.000Z",
                ))
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", rows)
            self.assertGreater(Path(path).stat().st_size, 8192)
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_NONE)
            self.assertEqual(info.get("completion_id"), "")

    def _user_response_item(self, uid: str, text: str, ts: str) -> dict:
        # Dual emission with the item stream (observed in real modern files);
        # also gives the public scan a readable first user message.
        return {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": uid,
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def test_public_scan_rejects_stale_terminal(self) -> None:
        """The exact public scan path from the rejection: isolated file with an
        earlier terminal plus newer turn activity reports nonterminal."""
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._meta()
            meta["payload"] = {"cwd": "/tmp", "thread_source": "user"}
            path = self._write(tmp, f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl", [
                meta,
                self._task_started(),
                self._user_response_item("u-1", "do it", "2026-10-01T03:58:34.000Z"),
                self._task_complete("2026-10-01T03:59:00.000Z"),
                {
                    "timestamp": "2026-10-01T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "next-turn"},
                },
                self._cmd_item("c-9", "2026-10-01T04:00:10.000Z"),
            ])

            class _NullCache:
                def get_session(self, *args, **kwargs):
                    return None

                def put_session(self, *args, **kwargs):
                    return None

            with mock.patch.object(codex, "_find_all_session_files", return_value=[path]), \
                mock.patch.object(codex, "_load_index", return_value={}), \
                mock.patch.object(codex, "_live_session_ids", return_value={}), \
                mock.patch.object(codex, "get_cache", return_value=_NullCache()):
                out = codex.scan_sessions(limit=10)
            self.assertEqual(len(out), 1)
            self.assertNotIn(out[0]["status_tag"],
                             (titles.STATUS_DONE, titles.STATUS_ABORTED))
            self.assertEqual(out[0].get("completion_id"), "")


class CodexLateSameTurnItemTests(unittest.TestCase):
    """A background command finishing after its turn ended must not reopen it (2026-10-07)."""

    SID = "01a114ca-4323-75a0-938d-048dfebb89d9"

    def _rows(self, tail: list[dict]) -> list[dict]:
        return [
            {"timestamp": "2026-10-07T09:40:00.000Z", "type": "session_meta",
             "payload": {"cwd": "/tmp/demo", "thread_source": "user"}},
            {"timestamp": "2026-10-07T09:40:01.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "do the thing"}},
            {"timestamp": "2026-10-07T09:40:02.000Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "turn-a"}},
            {"timestamp": "2026-10-07T09:48:30.000Z", "type": "event_msg",
             "payload": {"type": "item_completed", "turn_id": "turn-a",
                         "item": {"type": "AgentMessage", "id": "i1"}}},
            {"timestamp": "2026-10-07T09:49:07.000Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "turn-a",
                         "last_agent_message": None,
                         "error": {"message": "You've hit your usage limit."}}},
            *tail,
        ]

    def _info(self, tmp: str, name: str, tail: list[dict]) -> dict:
        path = Path(tmp) / f"rollout-2026-10-07T17-40-00-{self.SID}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in self._rows(tail)) + "\n", encoding="utf-8")
        info = codex._build_session_info(str(path), {})
        assert info is not None
        return info

    def test_late_completion_of_the_same_turn_keeps_the_abort(self) -> None:
        late = {"timestamp": "2026-10-07T09:50:57.000Z", "type": "event_msg",
                "payload": {"type": "item_completed", "turn_id": "turn-a",
                            "item": {"type": "CommandExecution", "id": "cmd1", "status": "completed"}}}
        with tempfile.TemporaryDirectory() as tmp:
            before = self._info(tmp, "a", [])
            after = self._info(tmp, "b", [late])
        self.assertEqual(before["status_tag"], titles.STATUS_ABORTED)
        self.assertEqual(after["status_tag"], titles.STATUS_ABORTED)
        self.assertTrue(after["completion_id"])
        self.assertEqual(after["completion_id"], before["completion_id"])

    def test_new_turn_activity_still_reopens(self) -> None:
        for kind, row in (
            ("started", {"timestamp": "2026-10-07T09:51:00.000Z", "type": "event_msg",
                         "payload": {"type": "task_started", "turn_id": "turn-b"}}),
            ("other-turn", {"timestamp": "2026-10-07T09:51:00.000Z", "type": "event_msg",
                            "payload": {"type": "item_completed", "turn_id": "turn-b",
                                        "item": {"type": "CommandExecution", "id": "cmd2"}}}),
            ("no-turn", {"timestamp": "2026-10-07T09:51:00.000Z", "type": "event_msg",
                         "payload": {"type": "item_completed",
                                     "item": {"type": "CommandExecution", "id": "cmd3"}}}),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                info = self._info(tmp, kind, [row])
                self.assertEqual(info["status_tag"], titles.STATUS_NONE)
                self.assertEqual(info["completion_id"], "")


class CodexTerminalIdentityTests(unittest.TestCase):
    """Native terminal identity: distinct per actual turn, stable per event.

    Rejection (2026-10-01): two same-session `task_complete` events with
    distinct turn ids/timestamps, identical final text and NO `payload.id`
    shared one identity, so the second genuine end never notified. Current
    native terminal records carry `turn_id` plus a timestamp instead of a
    payload id; missing id is live format, not static legacy history.
    """

    SID = "01a0f3e5-b4a0-7912-ba12-f37d06b39289"

    def _write(self, tmp: str, rows: list[dict]) -> str:
        path = str(Path(tmp) / f"rollout-2026-10-01T03-58-32-{self.SID}.jsonl")
        Path(path).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return path

    def _meta(self) -> dict:
        return {
            "timestamp": "2026-10-01T05:58:00.000Z",
            "type": "session_meta",
            "payload": {"cwd": "/tmp", "thread_source": "user"},
        }

    def _user(self) -> dict:
        return {
            "timestamp": "2026-10-01T05:59:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "synthetic task"},
        }

    def _started(self, turn: str, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn},
        }

    def _complete(self, turn: str, ts: str, text: str = "identical done") -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn,
                "completed_at": ts,
                "started_at": "2026-10-01T05:59:30.000Z",
                "last_agent_message": text,
            },
        }

    def _abort(self, turn: str, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": turn,
                "completed_at": ts,
                "started_at": "2026-10-01T05:59:30.000Z",
                "reason": "cancelled",
            },
        }

    def _error_complete(self, turn: str, ts: str, text: str = "limit hit") -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn,
                "completed_at": ts,
                "started_at": "2026-10-01T05:59:30.000Z",
                "last_agent_message": None,
                "error": {"message": text},
            },
        }

    def _token(self, ts: str) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "total": 7},
        }

    def _parse(self, path: str):
        info = codex._build_session_info(path, {})
        assert info is not None
        return info

    def test_first_native_turn_done_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                self._started("genuine-first", "2026-10-01T05:59:30.000Z"),
                self._complete("genuine-first", "2026-10-01T06:00:00.000Z"),
            ])
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.writelines(
                    json.dumps(self._token("2026-10-01T06:00:01.000Z")) + "\n"
                    for _ in range(10)
                )
            second = self._parse(path)
            self.assertEqual(first["completion_id"], second["completion_id"])
            third = self._parse(path)
            self.assertEqual(first["completion_id"], third["completion_id"])

    def test_second_native_turn_same_text_distinct_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                self._started("genuine-first", "2026-10-01T05:59:30.000Z"),
                self._complete("genuine-first", "2026-10-01T06:00:00.000Z"),
            ])
            first = self._parse(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    self._started("genuine-second", "2026-10-01T06:00:30.000Z")) + "\n")
                handle.write(json.dumps(
                    self._complete("genuine-second", "2026-10-01T06:01:00.000Z")) + "\n")
            second = self._parse(path)
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertTrue(second.get("completion_id"))
            self.assertNotEqual(first["completion_id"], second["completion_id"])

    def test_abort_identity_distinct_per_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                self._started("turn-a", "2026-10-01T05:59:30.000Z"),
                self._abort("turn-a", "2026-10-01T06:00:00.000Z"),
            ])
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(first.get("completion_id"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    self._started("turn-b", "2026-10-01T06:00:30.000Z")) + "\n")
                handle.write(json.dumps(
                    self._abort("turn-b", "2026-10-01T06:01:00.000Z")) + "\n")
            second = self._parse(path)
            self.assertEqual(second["status_tag"], titles.STATUS_ABORTED)
            self.assertNotEqual(first["completion_id"], second["completion_id"])
            self.assertEqual(second["completion_id"], self._parse(path)["completion_id"])

    def test_error_complete_identity_distinct_per_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                self._started("turn-a", "2026-10-01T05:59:30.000Z"),
                self._error_complete("turn-a", "2026-10-01T06:00:00.000Z"),
            ])
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(first.get("completion_id"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    self._started("turn-b", "2026-10-01T06:00:30.000Z")) + "\n")
                handle.write(json.dumps(
                    self._error_complete("turn-b", "2026-10-01T06:01:00.000Z")) + "\n")
            second = self._parse(path)
            self.assertEqual(second["status_tag"], titles.STATUS_ABORTED)
            self.assertNotEqual(first["completion_id"], second["completion_id"])

    def test_legacy_timestamp_fallback_stable_and_distinct(self) -> None:
        """No turn_id/id at all: outer timestamps anchor legacy terminal rows."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                self._meta(), self._user(),
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "PONG"},
                },
            ]
            path = self._write(tmp, rows)
            first = self._parse(path)
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))
            self.assertEqual(first["completion_id"], self._parse(path)["completion_id"])
            rows[-1] = {
                "timestamp": "2026-09-15T08:34:06.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "PONG"},
            }
            path = self._write(tmp, rows)
            second = self._parse(path)
            self.assertNotEqual(first["completion_id"], second["completion_id"])

    def test_bare_terminal_marker_has_empty_id(self) -> None:
        """A terminal marker with no id, turn, timestamp, or text fabricates nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info.get("completion_id"), "")

    def test_text_only_terminal_has_empty_id(self) -> None:
        """Terminal text present but every native anchor absent: still empty.

        Rejection case: `task_complete(last_agent_message='identical done')`
        with no id/turn_id/completed_at/outer timestamp must keep DONE display
        yet mint no identity.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                {"type": "event_msg",
                 "payload": {"type": "task_complete",
                             "last_agent_message": "identical done"}},
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info.get("completion_id"), "")
            self.assertEqual(info["last_agent_msg"], "identical done")

    def test_text_only_error_terminal_has_empty_id(self) -> None:
        """Same conservative gate for error-complete terminals."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                self._meta(), self._user(),
                {"type": "event_msg",
                 "payload": {"type": "task_complete",
                             "error": {"message": "limit hit"}}},
            ])
            info = self._parse(path)
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertEqual(info.get("completion_id"), "")


class PiCompletionIdTests(unittest.TestCase):
    def _pi_session(self, path: Path, leaf_text: str, stop_reason: str | None = "stop") -> Path:
        # v1 风格：message 无 id/parentId，按文件顺序平铺为活动分支。
        assistant = {"role": "assistant", "content": leaf_text}
        if stop_reason is not None:
            assistant["stopReason"] = stop_reason
        entries = [
            {
                "type": "session",
                "id": "pi-session-1",
                "cwd": "/tmp/demo",
                "timestamp": "2026-09-15T08:33:03.000Z",
            },
            {
                "type": "message",
                "timestamp": "2026-09-15T08:33:04.000Z",
                "message": {"role": "user", "content": "do the thing"},
            },
            {
                "type": "message",
                "timestamp": "2026-09-15T08:33:05.000Z",
                "message": assistant,
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in entries) + "\n", encoding="utf-8")
        return path

    def test_done_has_id_and_new_round_changes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pi_session(Path(tmp) / "s.jsonl", "hello")
            first, _ = pi._build_session_info(str(path))
            assert first is not None
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))

            again, _ = pi._build_session_info(str(path))
            assert again is not None
            self.assertEqual(first["completion_id"], again["completion_id"])

    def test_error_round_has_distinct_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok_path = self._pi_session(Path(tmp) / "ok.jsonl", "hello", "stop")
            ok, _ = pi._build_session_info(str(ok_path))
            err_path = self._pi_session(Path(tmp) / "err.jsonl", "", "error")
            # 空正文 + error 仍独占一轮（errorMessage 为空时 stopReason 兜底）
            raw = json.loads(err_path.read_text(encoding="utf-8").splitlines()[-1])
            raw["message"]["errorMessage"] = "429 weekly limit"
            raw["message"]["content"] = ""
            lines = err_path.read_text(encoding="utf-8").splitlines()
            lines[-1] = json.dumps(raw)
            err_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            err, _ = pi._build_session_info(str(err_path))
            assert ok is not None and err is not None
            self.assertEqual(err["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(err.get("completion_id"))
            self.assertNotEqual(ok["completion_id"], err["completion_id"])

    def test_nonterminal_tail_has_no_completion_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index, reason in enumerate(("toolUse", "length", "deferred", "pending", None)):
                with self.subTest(stop_reason=reason):
                    path = self._pi_session(Path(tmp) / f"unknown-{index}.jsonl", "not confirmed", reason)
                    info, _ = pi._build_session_info(str(path))
                    assert info is not None
                    self.assertEqual(info["status_tag"], titles.STATUS_NONE)
                    self.assertEqual(info["completion_id"], "")


class ClaudeFinalityTests(unittest.TestCase):
    """Claude progress/tool_use is not completion (native stop semantics).

    stop_reason=tool_use means the turn continues; only end_turn (or legacy
    rows without the field) with no later assistant/user/attachment activity
    is terminal. Metadata rows must not shift identity.
    """

    def _write(self, tmp: str, name: str, rows: list[dict]) -> str:
        path = str(Path(tmp) / name)
        Path(path).write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return path

    def _user(self, text: str, ts: str = "2026-09-30T20:00:00.000Z") -> dict:
        return {
            "type": "user",
            "message": {"role": "user", "content": text},
            "timestamp": ts,
            "uuid": "u-prompt",
        }

    def _assistant(
        self,
        text: str,
        stop: str | None,
        uuid: str,
        ts: str = "2026-09-30T20:01:00.000Z",
    ) -> dict:
        row: dict = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "timestamp": ts,
            "uuid": uuid,
            "parentUuid": "u-prompt",
        }
        if stop is not None:
            row["message"]["stop_reason"] = stop
        return row

    def _tool_result(self, ts: str = "2026-09-30T20:02:00.000Z") -> dict:
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "ok",
                    }
                ],
            },
            "timestamp": ts,
            "uuid": "u-toolresult",
        }

    def _meta(self, ts: str = "2026-09-30T20:03:00.000Z") -> dict:
        return {"type": "queue-operation", "op": "enqueue", "timestamp": ts}

    def test_tool_use_tail_is_not_done(self) -> None:
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("do the thing"),
                    self._assistant("working on it", "tool_use", "a-1"),
                    self._tool_result(),
                ],
            )
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertNotEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["completion_id"], "")

    def test_tool_only_tail_after_text_is_not_done(self) -> None:
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("do the thing"),
                    self._assistant("here is my plan", "tool_use", "a-1"),
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "Bash",
                                    "input": {},
                                }
                            ],
                        },
                        "timestamp": "2026-09-30T20:02:00.000Z",
                        "uuid": "a-2",
                    },
                ],
            )
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertNotEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["completion_id"], "")

    def test_truncation_is_not_done(self) -> None:
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("do the thing"),
                    self._assistant("partial answer", "max_tokens", "a-9"),
                ],
            )
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertNotEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["completion_id"], "")

    def test_end_turn_is_done_with_stable_uuid_anchor(self) -> None:
        import os as _os

        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                self._user("do the thing"),
                self._assistant("all done", "end_turn", "a-final"),
            ]
            path = self._write(tmp, "s.jsonl", rows)
            first = claude._build_session_info(path, tmp)
            assert first is not None
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))
            # Metadata-only append + touch: same genuine completion, same id.
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self._meta()) + "\n")
            _os.utime(path, (9999999999, 9999999999))
            second = claude._build_session_info(path, tmp)
            assert second is not None
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertEqual(first["completion_id"], second["completion_id"])

    def test_context_attachment_after_final_reply_keeps_done(self) -> None:
        """Claude Code 2.1.291 writes a prompt_snapshot after the final reply.

        Context-metadata attachments do not continue the turn; the identity
        stays anchored to the final reply. A queued human prompt still does.
        """
        from sesskit.parsers import claude

        snapshot = {
            "type": "attachment", "timestamp": "2026-09-30T20:01:01.000Z",
            "attachment": {"type": "prompt_snapshot", "contextRendering": "x"},
        }
        duration = {
            "type": "system", "subtype": "turn_duration",
            "timestamp": "2026-09-30T20:01:02.000Z",
        }
        queued = {
            "type": "attachment", "timestamp": "2026-09-30T20:01:03.000Z",
            "attachment": {"type": "queued_command", "prompt": "one more thing"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = [self._user("do the thing"), self._assistant("all done", "end_turn", "a-final")]
            plain = claude._build_session_info(self._write(tmp, "plain.jsonl", base), tmp)
            snap = claude._build_session_info(
                self._write(tmp, "snap.jsonl", [*base, snapshot, duration]), tmp,
            )
            assert plain is not None and snap is not None
            self.assertEqual(snap["status_tag"], titles.STATUS_DONE)
            self.assertEqual(snap["completion_id"], plain["completion_id"])
            more = claude._build_session_info(
                self._write(tmp, "queued.jsonl", [*base, snapshot, queued]), tmp,
            )
            assert more is not None
            self.assertNotEqual(more["status_tag"], titles.STATUS_DONE)
            self.assertEqual(more["completion_id"], "")

    def test_window_boundary_shift_keeps_terminal_identity(self) -> None:
        """Rolling 64 KB window losing the user excerpt must not change identity.

        2026-10-01 acceptance rejection: a 20 KB user prompt plus one final
        assistant event parses DONE; after ~45 KB of metadata rows push the
        user text out of the tail window, the rescan is still DONE with the
        identical terminal event/text but the id changed — one genuine
        completion, two notifications. Identity inputs must come from the
        anchored terminal event only, never from window excerpt availability.
        """
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("x" * 20000),
                    self._assistant("done", "end_turn", "a-final"),
                ],
            )
            first = claude._build_session_info(path, tmp)
            assert first is not None
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))
            self.assertTrue(first["last_user_msg"])
            with open(path, "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps({"type": "progress", "data": "m" * 500}) + "\n" for _ in range(90))
            self.assertGreater(Path(path).stat().st_size, 65536)
            second = claude._build_session_info(path, tmp)
            assert second is not None
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertEqual(second["last_agent_msg"], "done")
            # The user excerpt is genuinely gone from the rolling tail now.
            self.assertFalse(second["last_user_msg"])
            # Same terminal event, same text: same id; rescan again for restart.
            self.assertEqual(first["completion_id"], second["completion_id"])
            third = claude._build_session_info(path, tmp)
            assert third is not None
            self.assertEqual(first["completion_id"], third["completion_id"])

    def test_error_identity_survives_window_shift(self) -> None:
        """ABORTED error identity also comes from the anchored error event."""
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("y" * 20000),
                    {
                        "type": "system",
                        "error": {
                            "formatted": "Credit balance too low",
                            "status": 402,
                        },
                        "timestamp": "2026-09-30T20:02:00.000Z",
                        "uuid": "e-1",
                    },
                ],
            )
            first = claude._build_session_info(path, tmp)
            assert first is not None
            self.assertEqual(first["status_tag"], titles.STATUS_ABORTED)
            with open(path, "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps({"type": "progress", "data": "m" * 500}) + "\n" for _ in range(90))
            second = claude._build_session_info(path, tmp)
            assert second is not None
            self.assertEqual(second["status_tag"], titles.STATUS_ABORTED)
            self.assertEqual(first["completion_id"], second["completion_id"])

    def test_same_text_different_turns_have_distinct_ids(self) -> None:
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            one = self._write(
                tmp,
                "one.jsonl",
                [
                    self._user("do the thing"),
                    self._assistant("all done", "end_turn", "a-turn-1"),
                ],
            )
            two = self._write(
                tmp,
                "two.jsonl",
                [
                    self._user("do the other thing"),
                    self._assistant("all done", "end_turn", "a-turn-2"),
                ],
            )
            first = claude._build_session_info(one, tmp)
            second = claude._build_session_info(two, tmp)
            assert first is not None and second is not None
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertEqual(second["status_tag"], titles.STATUS_DONE)
            self.assertNotEqual(first["completion_id"], second["completion_id"])

    def test_aborted_stays_aborted_with_id(self) -> None:
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("do the thing"),
                    {
                        "type": "system",
                        "error": {
                            "formatted": "Credit balance too low",
                            "status": 402,
                        },
                        "timestamp": "2026-09-30T20:02:00.000Z",
                        "uuid": "e-1",
                    },
                ],
            )
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(info.get("completion_id"))

    def test_legacy_missing_stop_reason_is_done(self) -> None:
        """Old history rows without the stop_reason field stay terminal."""
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "s.jsonl",
                [
                    self._user("do the thing"),
                    self._assistant("all done", None, "a-legacy"),
                ],
            )
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertTrue(info.get("completion_id"))

    def test_null_stop_reason_is_not_done(self) -> None:
        """Field present but null: missing finality evidence, never DONE."""
        from sesskit.parsers import claude

        with tempfile.TemporaryDirectory() as tmp:
            row = self._assistant("all done", None, "a-null")
            row["message"]["stop_reason"] = None
            path = self._write(tmp, "s.jsonl", [self._user("do the thing"), row])
            info = claude._build_session_info(path, tmp)
            assert info is not None
            self.assertNotEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["completion_id"], "")


if __name__ == "__main__":
    unittest.main()
