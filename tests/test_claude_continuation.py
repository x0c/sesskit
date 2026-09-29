"""Claude `continued-in` session continuation (incident 2026-09-30).

A conversation may move to a new session file while the old id stays
resumable: the old file carries
``{"type":"continued-in","sessionId":<old>,"continuedInSessionId":<new>}``
and the new file continues the messages. Scanners must expose the forward
pointer as a typed relation plus ``superseded_by`` (only when the target
file exists); resume paths resolve the chain to the latest readable id.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sesskit.parsers import claude
from sesskit.relations import (
    claude_continuation_target,
    resolve_continuation,
    session_relations,
)


def _user(sid: str, text: str, cwd: str) -> str:
    return json.dumps({
        "type": "user", "cwd": cwd, "sessionId": sid,
        "timestamp": "2026-09-30T00:00:01.000Z",
        "message": {"role": "user", "content": text},
    })


def _assistant(sid: str, text: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant", "cwd": cwd, "sessionId": sid,
        "timestamp": "2026-09-30T00:00:02.000Z",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    })


def _continued(old: str, new: str) -> str:
    return json.dumps({
        "type": "continued-in", "sessionId": old,
        "continuedInSessionId": new,
        "timestamp": "2026-09-30T00:00:03.000Z",
    })


def _write_session(folder: Path, sid: str, lines: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


class ContinuationFixture:
    """Two-session project dir: old file continued into new file."""

    OLD = "aaaaaaaa-0000-4000-8000-000000000001"
    NEW = "bbbbbbbb-0000-4000-8000-000000000002"

    def __init__(self, root: Path, *, pointer_line: int = 3,
                 filler_lines: int = 0, target_missing: bool = False):
        self.cwd = root / "work"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.folder = root / "projects" / "-work"
        lines = [_user(self.OLD, "first task", str(self.cwd)),
                 _assistant(self.OLD, "working on it", str(self.cwd))]
        for idx in range(filler_lines):
            lines.append(_user(self.OLD, f"filler {idx}", str(self.cwd)))
        lines.append(_continued(self.OLD, self.NEW))
        lines.append(_assistant(self.OLD, "tail after fork", str(self.cwd)))
        if pointer_line > len(lines):
            # Pad so the pointer sits past the head/tail scan windows.
            lines[2:2] = [_user(self.OLD, f"pad {idx}", str(self.cwd))
                          for idx in range(pointer_line - len(lines))]
        self.old_path = _write_session(self.folder, self.OLD, lines)
        if not target_missing:
            self.new_path = _write_session(
                self.folder, self.NEW,
                [_user(self.NEW, "continued task", str(self.cwd)),
                 _assistant(self.NEW, "continued work", str(self.cwd))])
        else:
            self.new_path = self.folder / f"{self.NEW}.jsonl"

    def scan(self, limit: int = 10) -> list[dict]:
        with mock.patch.object(claude, "PROJECTS_DIR", str(self.folder.parent)), \
                mock.patch.object(claude, "SESSIONS_DIR", str(self.folder.parent / "nosuch")), \
                mock.patch.object(claude, "_live_session_ids", return_value={}):
            return claude.scan_sessions(limit=limit)


class ContinuationRelationTests(unittest.TestCase):
    def test_continuation_relation_emitted_with_native_evidence(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            relations = session_relations(
                {"source": "claude", "id": fix.OLD, "path": str(fix.old_path)})
        continued = [r for r in relations if r.kind == "continuation"]
        self.assertEqual(len(continued), 1)
        self.assertEqual(continued[0].target, fix.NEW)
        self.assertEqual(continued[0].evidence.origin, "native")

    def test_no_pointer_no_continuation_relation(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            relations = session_relations(
                {"source": "claude", "id": fix.NEW, "path": str(fix.new_path)})
        self.assertEqual([r for r in relations if r.kind == "continuation"], [])

    def test_target_helper_reads_last_pointer(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "s.jsonl"
            path.write_text(_continued("x", "first-target") + "\n"
                            + _continued("x", "last-target") + "\n")
            self.assertEqual(claude_continuation_target(str(path)), "last-target")
            self.assertIsNone(claude_continuation_target(str(root / "missing.jsonl")))


class ContinuationScanTests(unittest.TestCase):
    def test_scan_marks_superseded_only_when_target_exists(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            sessions = fix.scan()
        by_id = {s["id"]: s for s in sessions}
        self.assertIn(fix.OLD, by_id)
        self.assertIn(fix.NEW, by_id)
        self.assertEqual(by_id[fix.OLD].get("superseded_by"), fix.NEW)
        self.assertNotIn("superseded_by", by_id[fix.NEW])

    def test_scan_missing_target_stays_unknown(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td), target_missing=True)
            sessions = fix.scan()
        by_id = {s["id"]: s for s in sessions}
        self.assertIn(fix.OLD, by_id)
        self.assertNotIn("superseded_by", by_id[fix.OLD])

    def test_pointer_outside_head_tail_windows_is_found(self):
        # Head covers 300 lines, tail 64 KB: bury the pointer past both.
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td), pointer_line=400,
                                      filler_lines=1200)
            sessions = fix.scan()
        by_id = {s["id"]: s for s in sessions}
        self.assertEqual(by_id[fix.OLD].get("superseded_by"), fix.NEW)

    def test_self_pointer_is_quarantined(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            cwd = root / "work"
            cwd.mkdir()
            folder = root / "projects" / "-work"
            sid = ContinuationFixture.OLD
            path = _write_session(folder, sid, [
                _user(sid, "task", str(cwd)),
                _continued(sid, sid),
            ])
            with mock.patch.object(claude, "PROJECTS_DIR", str(folder.parent)), \
                    mock.patch.object(claude, "SESSIONS_DIR", str(folder.parent / "nosuch")), \
                    mock.patch.object(claude, "_live_session_ids", return_value={}):
                sessions = claude.scan_sessions(limit=10)
            by_id = {s["id"]: s for s in sessions}
            self.assertNotIn("superseded_by", by_id[sid])
            self.assertEqual(
                resolve_continuation({"source": "claude", "id": sid,
                                      "path": str(path)}), sid)

    def test_bg_continuation_file_with_early_agent_name_is_listed(self):
        # Background-continuation files open with a metadata block
        # (ai-title/agent-name/...) stamped with the new id before the
        # first user message; the early agent-name must not mark them
        # as internal teammates sessions.
        with TemporaryDirectory() as td:
            root = Path(td)
            cwd = root / "work"
            cwd.mkdir()
            folder = root / "projects" / "-work"
            old, new = ContinuationFixture.OLD, ContinuationFixture.NEW
            _write_session(folder, old, [
                _user(old, "first task", str(cwd)),
                _assistant(old, "working on it", str(cwd)),
                _continued(old, new),
            ])
            new_lines = [
                json.dumps({"type": "ai-title", "sessionId": new,
                            "aiTitle": "bg work"}),
                json.dumps({"type": "agent-name", "sessionId": new,
                            "agentName": "human readable name"}),
                json.dumps({"type": "last-prompt", "sessionId": new,
                            "lastPrompt": "first task"}),
                json.dumps({"type": "attachment", "sessionId": new,
                            "session_id": old, "cwd": str(cwd),
                            "timestamp": "2026-09-30T00:00:01.000Z",
                            "parentUuid": "x", "uuid": "y",
                            "attachment": {"type": "queued_command",
                                           "prompt": "n/a"}}),
                _user(new, "continued task", str(cwd)),
                _assistant(new, "continued work", str(cwd)),
            ]
            _write_session(folder, new, new_lines)
            with mock.patch.object(claude, "PROJECTS_DIR", str(folder.parent)), \
                    mock.patch.object(claude, "SESSIONS_DIR", str(folder.parent / "nosuch")), \
                    mock.patch.object(claude, "_live_session_ids", return_value={}):
                sessions = claude.scan_sessions(limit=10)
            by_id = {s["id"]: s for s in sessions}
            self.assertIn(new, by_id)
            self.assertEqual(by_id[old].get("superseded_by"), new)

    def test_true_internal_session_still_filtered(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            cwd = root / "work"
            cwd.mkdir()
            folder = root / "projects" / "-work"
            sid = "cccccccc-0000-4000-8000-000000000003"
            _write_session(folder, sid, [
                json.dumps({"type": "agent-name", "sessionId": sid,
                            "agentName": "teammate-x"}),
                _user(sid, "first task", str(cwd)),
            ])
            with mock.patch.object(claude, "PROJECTS_DIR", str(folder.parent)), \
                    mock.patch.object(claude, "SESSIONS_DIR", str(folder.parent / "nosuch")), \
                    mock.patch.object(claude, "_live_session_ids", return_value={}):
                sessions = claude.scan_sessions(limit=10)
            self.assertNotIn(sid, {s["id"] for s in sessions})


class ContinuationResolveTests(unittest.TestCase):
    def _chain(self, root: Path, ids: list[str]) -> Path:
        cwd = root / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        folder = root / "projects" / "-work"
        for pos, sid in enumerate(ids):
            lines = [_user(sid, f"task {pos}", str(cwd))]
            if pos + 1 < len(ids):
                lines.append(_continued(sid, ids[pos + 1]))
            _write_session(folder, sid, lines)
        return folder

    def test_chain_resolves_to_latest(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            folder = self._chain(root, ["id-a", "id-b", "id-c"])
            session = {"source": "claude", "id": "id-a",
                       "path": str(folder / "id-a.jsonl")}
            self.assertEqual(resolve_continuation(session), "id-c")

    def test_cycle_stops_at_last_readable(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            folder = self._chain(root, ["id-a", "id-b"])
            (folder / "id-b.jsonl").write_text(
                _user("id-b", "task", str(root / "work")) + "\n"
                + _continued("id-b", "id-a") + "\n")
            session = {"source": "claude", "id": "id-a",
                       "path": str(folder / "id-a.jsonl")}
            self.assertEqual(resolve_continuation(session), "id-b")

    def test_missing_hop_stops(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            folder = self._chain(root, ["id-a"])
            (folder / "id-a.jsonl").write_text(
                _user("id-a", "task", str(root / "work")) + "\n"
                + _continued("id-a", "id-gone") + "\n")
            session = {"source": "claude", "id": "id-a",
                       "path": str(folder / "id-a.jsonl")}
            self.assertEqual(resolve_continuation(session), "id-a")

    def test_non_claude_session_returns_own_id(self):
        session = {"source": "codex", "id": "thread-1", "path": "/nonexistent"}
        self.assertEqual(resolve_continuation(session), "thread-1")


class RealHistoryContinuationTests(unittest.TestCase):
    def test_real_history_pointer_counts(self):
        projects = Path(os.path.expanduser("~/.claude/projects"))
        if not projects.is_dir():
            self.skipTest("no local Claude history")
        files = list(projects.glob("*/*.jsonl"))
        if not files:
            self.skipTest("no local Claude session files")
        with_pointers = 0
        for path in files:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if '"continued-in"' in line:
                            with_pointers += 1
                            break
            except OSError:
                continue
        print(f"\nclaude files={len(files)} with-continued-in={with_pointers}")
        # Incident pair (2026-09-30): old file continued into a bg session.
        old = "c544eb92-a0b5-42f7-91a1-43dc71383bbb"
        new = "304638c0-56ec-46b7-bb62-51c45bf92f85"
        old_path = projects / "-Users-geraltgraham-Codes-Corral" / f"{old}.jsonl"
        if not old_path.is_file():
            self.skipTest("incident session files not present")
        self.assertEqual(claude_continuation_target(str(old_path)), new)
        self.assertEqual(
            resolve_continuation({"source": "claude", "id": old,
                                  "path": str(old_path)}), new)
        sessions = claude.scan_sessions(limit=50)
        by_id = {s["id"]: s for s in sessions}
        if old in by_id:
            self.assertEqual(by_id[old].get("superseded_by"), new)


if __name__ == "__main__":
    unittest.main()
