"""completion_id: per-round stable identity for completion notifications."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
        """Two genuine turns with identical text: native ids keep them distinct (D3)."""
        with tempfile.TemporaryDirectory() as tmp:
            infos = []
            for index, (sid, msg_id) in enumerate(
                [
                    ("01a0a432-a844-7300-951f-c6cc548cb301", "msg-aaa"),
                    ("01a0a432-a844-7300-951f-c6cc548cb302", "msg-bbb"),
                ]
            ):
                path = _codex_session(
                    Path(tmp) / f"rollout-2026-09-15T16-32-5{index}-{sid}.jsonl",
                    sid,
                    {
                        "timestamp": "2026-09-15T08:33:06.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "id": msg_id,
                            "content": [{"type": "output_text", "text": "all done"}],
                        },
                    },
                )
                info = codex._build_session_info(str(path), {})
                assert info is not None
                self.assertEqual(info["status_tag"], titles.STATUS_DONE)
                self.assertTrue(info.get("completion_id"))
                infos.append(info)
            self.assertNotEqual(infos[0]["completion_id"], infos[1]["completion_id"])


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
