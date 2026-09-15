import unittest
from unittest import mock

from sesskit.parsers import codex


class CodexLivenessTests(unittest.TestCase):
    def test_macos_shared_snapshot_identifies_open_rollout(self):
        sid = "01a09e28-9004-7ae0-a03b-256ee1f9943c"
        output = f"p72684\nn/tmp/rollout-2026-09-14T12-24-13-{sid}.jsonl\n"
        with mock.patch.object(codex.sys, "platform", "darwin"), \
             mock.patch.object(codex, "live_pid_snapshot", return_value=(72684,)), \
             mock.patch.object(codex.subprocess, "check_output", return_value=output.encode()) as probe:
            self.assertEqual(codex._live_session_ids(), {sid: 72684})
        self.assertEqual(probe.call_args.args[0], ["lsof", "-n", "-P", "-Fpn", "-p", "72684"])

    def test_empty_snapshot_does_not_scan_unrelated_open_files(self):
        with mock.patch.object(codex, "live_pid_snapshot", return_value=()), \
             mock.patch.object(codex.subprocess, "check_output") as probe:
            self.assertEqual(codex._live_session_ids(), {})
        probe.assert_not_called()
