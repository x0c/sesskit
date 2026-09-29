"""Disposable automation workspaces never list (docs/CONTRACT.md "Listing modes")."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit.parsers import claude
from sesskit.parsers.common import is_ephemeral_agent_cwd


class EphemeralCwdTests(unittest.TestCase):
    def test_oc_manager_segment(self):
        self.assertTrue(is_ephemeral_agent_cwd("/tmp/oc-manager-codex/run-1"))
        self.assertFalse(is_ephemeral_agent_cwd("/tmp/other-project"))
        self.assertFalse(is_ephemeral_agent_cwd(""))

    def test_marker_in_cwd_or_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "butler-e1" / "claude-1790653232"
            run.mkdir(parents=True)
            self.assertFalse(is_ephemeral_agent_cwd(str(run)))
            (run.parent / ".sesskit-ignore").touch()
            self.assertTrue(is_ephemeral_agent_cwd(str(run)))
            self.assertFalse(is_ephemeral_agent_cwd(td))
            with mock.patch.dict(os.environ, {"SESSKIT_INCLUDE_EPHEMERAL": "1"}):
                self.assertFalse(is_ephemeral_agent_cwd(str(run)))


class ClaudeScanTests(unittest.TestCase):
    def test_marked_workspace_session_is_not_listed(self):
        with tempfile.TemporaryDirectory() as td:
            projects = Path(td) / "projects"
            real = Path(td) / "real"
            spike = Path(td) / "spike" / "claude-1"
            real.mkdir()
            spike.mkdir(parents=True)
            (spike.parent / ".sesskit-ignore").touch()
            for idx, cwd in enumerate((real, spike)):
                folder = projects / str(cwd).replace("/", "-")
                folder.mkdir(parents=True)
                sid = f"0d633b5b-1ec5-40f5-908b-f7c95efbb78{idx}"
                entry = {
                    "type": "user", "cwd": str(cwd), "sessionId": sid,
                    "timestamp": "2026-09-29T03:40:37.385Z",
                    "message": {"role": "user", "content": f"task {idx}"},
                }
                (folder / f"{sid}.jsonl").write_text(json.dumps(entry) + "\n")
            with mock.patch.object(claude, "PROJECTS_DIR", str(projects)), \
                    mock.patch.object(claude, "_live_session_ids", return_value={}):
                sessions = claude.scan_sessions(limit=10)
        self.assertEqual([s["cwd"] for s in sessions], [str(real)])


if __name__ == "__main__":
    unittest.main()
