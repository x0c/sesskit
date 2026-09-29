import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit.parsers import codex


class CodexHostClaimTests(unittest.TestCase):
    session_id = "019efe42-6d51-7fb3-ad48-112a8eefaa01"

    def _write_session(self, root: Path, *, include_user_message: bool = True) -> None:
        path = root / f"rollout-2026-07-16T10-00-00-{self.session_id}.jsonl"
        entries = [
            {
                "timestamp": "2026-07-16T02:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": self.session_id, "cwd": str(root)},
            }
        ]
        if include_user_message:
            entries.append(
                {
                    "timestamp": "2026-07-16T02:00:10.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "fixture prompt"},
                }
            )
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")

    def _scan(self, root: Path, live_ids: dict[str, int], **kwargs):
        index_path = root / "session_index.jsonl"
        cache = mock.Mock()
        cache.get_session.return_value = None
        with (
            mock.patch.object(codex, "SESSIONS_DIR", str(root)),
            mock.patch.object(codex, "SESSION_INDEX", str(index_path)),
            mock.patch.object(codex, "_live_session_ids", return_value=live_ids),
            mock.patch.object(codex, "get_cache", return_value=cache),
            mock.patch.object(codex, "is_ephemeral_agent_cwd", return_value=False),
        ):
            return codex.scan_sessions(limit=10, **kwargs)

    def test_default_scan_uses_native_process_evidence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_session(root)

            sessions = self._scan(root, {self.session_id: 12345})

        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 12345)

    def test_injected_host_claim_takes_precedence_over_native_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_session(root)
            provider = mock.Mock(return_value={self.session_id: 67890})

            sessions = self._scan(
                root,
                {self.session_id: 12345},
                host_claim_provider=provider,
            )

        provider.assert_called_once_with(str(root))
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 67890)

    def test_empty_history_requires_native_or_injected_live_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_session(root, include_user_message=False)

            sessions = self._scan(root, {})

        self.assertEqual(sessions, [])


if __name__ == "__main__":
    unittest.main()
