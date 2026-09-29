"""Optional per-scan host extension: neutral defaults, fake-host injection,
malformed data tolerance, and per-call isolation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sesskit.parsers import codex
from sesskit.parsers import pi as scan_pi
from sesskit.parsers.common import HostExtension
from sesskit.registry import ParserRegistry, RuntimeParser
from sesskit.titles import is_title_generation_prompt

CODE_XID = "019efe42-6d51-7fb3-ad48-112a8eefaa01"


def _write_codex_session(root: Path, session_id: str, prompt: str, cwd: str) -> Path:
    path = root / f"rollout-2026-07-16T10-00-00-{session_id}.jsonl"
    entries = [
        {
            "timestamp": "2026-07-16T02:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        },
        {
            "timestamp": "2026-07-16T02:00:10.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": prompt},
        },
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _scan_codex(root: Path, live_ids: dict, **kwargs):
    index_path = root / "session_index.jsonl"
    cache = mock.Mock()
    cache.get_session.return_value = None
    with (
        mock.patch.object(codex, "SESSIONS_DIR", str(root)),
        mock.patch.object(codex, "SESSION_INDEX", str(index_path)),
        mock.patch.object(codex, "_live_session_ids", return_value=dict(live_ids)),
        mock.patch.object(codex, "get_cache", return_value=cache),
    ):
        return codex.scan_sessions(limit=10, **kwargs)


def _write_pi_session(directory: Path, session_id: str, cwd: str, text: str) -> Path:
    path = directory / f"2026-09-29T00-00-00-000Z_{session_id}.jsonl"
    entries = [
        {"type": "session", "id": session_id, "cwd": cwd, "timestamp": "2026-09-29T00:00:00Z"},
        {
            "id": "m1",
            "type": "message",
            "timestamp": "2026-09-29T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        },
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _scan_pi(directory: Path, procs: list, host=None, envs: dict | None = None):
    with (
        mock.patch.object(scan_pi, "SESSIONS_DIR", str(directory)),
        mock.patch.object(scan_pi, "live_processes", return_value=list(procs)),
        mock.patch.object(scan_pi, "process_command_line", return_value="pi --approve"),
        mock.patch.object(scan_pi, "open_file_paths", return_value={pid: [] for pid, _ in procs}),
        mock.patch.object(scan_pi, "process_start_time", return_value=None),
        mock.patch.object(
            scan_pi,
            "process_environ",
            side_effect=lambda pid, **kwargs: dict(envs.get(pid, {}) if envs else {}),
        ),
    ):
        scan_pi.reset_live_session_overrides()
        try:
            return scan_pi.scan_sessions(limit=10, host=host)
        finally:
            scan_pi.reset_live_session_overrides()


class NeutralDefaultTests(unittest.TestCase):
    def test_title_marker_needs_explicit_marker(self) -> None:
        self.assertFalse(is_title_generation_prompt("hello", marker=None))
        self.assertTrue(is_title_generation_prompt("hello MARK", marker="MARK"))
        self.assertFalse(is_title_generation_prompt("hello", marker="MARK"))
        # Legacy omission keeps old behavior for older direct callers only.
        self.assertTrue(is_title_generation_prompt("见你将看到一批编程助手会话的摘录x"))

    def test_codex_scan_without_host_is_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "FAKE-MARKER noise prompt", cwd)
            sessions = _scan_codex(root, {})
        # No host marker filtering and no automation-prefix exclusion.
        self.assertEqual(len(sessions), 1)
        self.assertIn("FAKE-MARKER", sessions[0]["first_user_msg"])

    def test_codex_oc_manager_dir_lists_without_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "oc-manager-job" / "run-1")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "real prompt", cwd)
            sessions = _scan_codex(root, {})
        self.assertEqual(len(sessions), 1)


class FakeHostTests(unittest.TestCase):
    def test_host_marker_filters_and_prefix_excludes(self) -> None:
        host = HostExtension(
            name="fake",
            title_prompt_marker="FAKE-MARKER",
            ephemeral_prefixes=("oc-manager-",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = str(root / "proj")
            auto = str(root / "oc-manager-job" / "run-1")
            os.makedirs(good)
            os.makedirs(auto)
            _write_codex_session(root, CODE_XID, "FAKE-MARKER noise prompt", good)
            other = "aaaaaaaa-1111-2222-3333-444444444444"
            _write_codex_session(root, other, "real prompt", auto)
            sessions = _scan_codex(root, {}, host=host)
        self.assertEqual(sessions, [])

    def test_host_claim_provider_merges(self) -> None:
        host = HostExtension(
            name="fake",
            codex_claim_provider=lambda sessions_dir: {CODE_XID: 67890},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "real prompt", cwd)
            sessions = _scan_codex(root, {CODE_XID: 12345}, host=host)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 67890)

    def test_explicit_provider_wins_over_host(self) -> None:
        host = HostExtension(
            name="fake",
            codex_claim_provider=lambda sessions_dir: {CODE_XID: 111},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "real prompt", cwd)
            sessions = _scan_codex(
                root, {}, host=host, host_claim_provider=lambda d: {CODE_XID: 222}
            )
        self.assertEqual(sessions[0]["pid"], 222)

    def test_host_cache_is_used_per_scan(self) -> None:
        gotten: list = []
        put: list = []

        class RecCache:
            def __init__(self):
                self.store: dict = {}

            def get_session(self, runtime, path, extra_version=""):
                gotten.append(extra_version)
                return self.store.get((runtime, path, extra_version))

            def put_session(self, runtime, path, payload, extra_version=""):
                put.append(extra_version)
                self.store[(runtime, path, extra_version)] = payload

            def get_conversation(self, runtime, key, path):
                ...

            def put_conversation(self, runtime, key, path, messages):
                ...

        host = HostExtension(name="fake", cache=RecCache())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "real prompt", cwd)
            sessions = _scan_codex(root, {}, host=host)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(gotten and put)
        global_cache = mock.Mock()
        global_cache.get_session.return_value = None
        with (
            mock.patch.object(codex, "SESSIONS_DIR", str(root)),
            mock.patch.object(codex, "SESSION_INDEX", str(root / "session_index.jsonl")),
            mock.patch.object(codex, "_live_session_ids", return_value={}),
            mock.patch.object(codex, "get_cache", return_value=global_cache),
        ):
            codex.scan_sessions(limit=10, host=host)
        global_cache.get_session.assert_not_called()
        global_cache.put_session.assert_not_called()


class CacheIsolationTests(unittest.TestCase):
    def test_different_hosts_do_not_share_cached_records(self) -> None:
        versions: set = []

        class RecCache:
            def __init__(self):
                self.store: dict = {}

            def get_session(self, runtime, path, extra_version=""):
                versions.append(extra_version)
                return self.store.get((runtime, path, extra_version))

            def put_session(self, runtime, path, payload, extra_version=""):
                self.store[(runtime, path, extra_version)] = dict(payload)

            def get_conversation(self, runtime, key, path):
                ...

            def put_conversation(self, runtime, key, path, messages):
                ...

        cache = RecCache()
        host_a = HostExtension(name="a", excerpt_preprocess=lambda s: "A:" + s)
        host_b = HostExtension(name="b", excerpt_preprocess=lambda s: "B:" + s)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, "real prompt", cwd)
            with (
                mock.patch.object(codex, "SESSIONS_DIR", str(root)),
                mock.patch.object(codex, "SESSION_INDEX", str(root / "session_index.jsonl")),
                mock.patch.object(codex, "_live_session_ids", return_value={}),
                mock.patch.object(codex, "get_cache", return_value=cache),
            ):
                first_a = codex.scan_sessions(limit=10, host=host_a)
                second_a = codex.scan_sessions(limit=10, host=host_a)
                first_b = codex.scan_sessions(limit=10, host=host_b)
                neutral = codex.scan_sessions(limit=10)
        self.assertTrue(first_a[0]["first_user_msg"].startswith("A:"))
        self.assertEqual(first_a[0]["first_user_msg"], second_a[0]["first_user_msg"])
        self.assertTrue(first_b[0]["first_user_msg"].startswith("B:"))
        self.assertFalse(neutral[0]["first_user_msg"].startswith(("A:", "B:")))
        self.assertGreaterEqual(len(set(versions)), 3)


class PiClaimExtensionTests(unittest.TestCase):
    def test_no_host_means_no_claim_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_pi_session(root, "pi-sess-1", cwd, "hello pi")
            sessions = _scan_pi(root, [(777, os.path.realpath(cwd))])
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0]["live"])

    def test_fake_claim_binds_on_explicit_evidence(self) -> None:
        host = HostExtension(
            name="fake",
            pi_instance_env_key="FAKE_INSTANCE",
            pi_claims_provider=lambda: [
                {"pid": 777, "session": "pi-sess-1", "sequence": 3, "instance": "i1"}
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_pi_session(root, "pi-sess-1", cwd, "hello pi")
            sessions = _scan_pi(
                root, [(777, os.path.realpath(cwd))], host=host, envs={777: {"FAKE_INSTANCE": "i1"}}
            )
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 777)

    def test_malformed_claims_never_bind_or_crash(self) -> None:
        host = HostExtension(
            name="fake",
            pi_instance_env_key="FAKE_INSTANCE",
            pi_claims_provider=lambda: [
                {"pid": "not-a-pid"},
                {"nope": True},
                "just-a-string",
                {"pid": -5, "session": "pi-sess-1"},
                {"pid": 777},
                {"pid": 777, "session": "unknown-session-not-in-window"},
                None,
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_pi_session(root, "pi-sess-1", cwd, "hello pi")
            sessions = _scan_pi(
                root, [(777, os.path.realpath(cwd))], host=host, envs={777: {"FAKE_INSTANCE": "i1"}}
            )
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0]["live"])

    def test_conflicting_claims_newest_sequence_wins(self) -> None:
        host = HostExtension(
            name="fake",
            pi_instance_env_key="FAKE_INSTANCE",
            pi_claims_provider=lambda: [
                {"pid": 777, "session": "pi-sess-1", "sequence": 1, "instance": "i1"},
                {"pid": 777, "session": "pi-sess-2", "sequence": 9, "instance": "i1"},
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_pi_session(root, "pi-sess-1", cwd, "hello pi")
            _write_pi_session(root, "pi-sess-2", cwd, "hello again")
            sessions = _scan_pi(
                root, [(777, os.path.realpath(cwd))], host=host, envs={777: {"FAKE_INSTANCE": "i1"}}
            )
        live = [s for s in sessions if s["live"]]
        self.assertEqual([s["id"] for s in live], ["pi-sess-2"])

    def test_instance_mismatch_stays_unbound(self) -> None:
        host = HostExtension(
            name="fake",
            pi_instance_env_key="FAKE_INSTANCE",
            pi_claims_provider=lambda: [
                {"pid": 777, "session": "pi-sess-1", "sequence": 1, "instance": "other"}
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_pi_session(root, "pi-sess-1", cwd, "hello pi")
            sessions = _scan_pi(
                root, [(777, os.path.realpath(cwd))], host=host, envs={777: {"FAKE_INSTANCE": "i1"}}
            )
        self.assertFalse(any(s["live"] for s in sessions))


class RegistryForwardingTests(unittest.TestCase):
    def test_host_forwarded_only_when_accepted(self) -> None:
        seen: dict = {}

        def new_scan(limit: int = 50, host=None):
            seen["host"] = host
            return []

        def old_scan(limit: int = 50):
            return []

        host = HostExtension(name="fake")
        parser = ParserRegistry(
            [
                RuntimeParser(id="a", display_name="A", _scan=new_scan, _load=lambda p: []),
                RuntimeParser(id="b", display_name="B", _scan=old_scan, _load=lambda p: []),
            ]
        )
        parser.get("a").scan_sessions(5, host=host)
        self.assertIs(seen["host"], host)
        parser.get("b").scan_sessions(5, host=host)
        out = parser.scan_all(5, host=host)
        self.assertEqual(out, {"a": [], "b": []})


class ProcessEnvironKeysTests(unittest.TestCase):
    def test_declared_keys_only(self) -> None:
        from sesskit.parsers import common

        line = "4242 /usr/bin/agent SESSKIT_SESSION_ID=sess-1 FAKE_HOST_KEY=abc OTHER=1"
        pid = 424242
        common._PROC_ENVIRON_CACHE.pop(pid, None)
        try:
            with (
                mock.patch.object(common.sys, "platform", "darwin"),
                mock.patch.object(
                    common.subprocess, "check_output", return_value=line.encode()
                ),
            ):
                env = common.process_environ(pid)
            self.assertEqual(env.get("SESSKIT_SESSION_ID"), "sess-1")
            self.assertNotIn("FAKE_HOST_KEY", env)
            self.assertNotIn("OTHER", env)
            common._PROC_ENVIRON_CACHE.pop(pid, None)
            with (
                mock.patch.object(common.sys, "platform", "darwin"),
                mock.patch.object(
                    common.subprocess, "check_output", return_value=line.encode()
                ),
            ):
                env = common.process_environ(pid, extra_keys=("FAKE_HOST_KEY",))
            self.assertEqual(env.get("FAKE_HOST_KEY"), "abc")
            self.assertNotIn("OTHER", env)
        finally:
            common._PROC_ENVIRON_CACHE.pop(pid, None)


class CacheDirTests(unittest.TestCase):
    def test_neutral_defaults_and_overrides(self) -> None:
        from sesskit.cache import cache_dir

        base = {"HOME": "/tmp/fakehome"}
        with mock.patch.dict(os.environ, {**base, "CORRAL_CACHE_DIR": "/tmp/z"}, clear=True):
            default = cache_dir()
        self.assertNotIn("should-be-ignored", str(default))
        self.assertNotIn("z", str(default))
        self.assertTrue(str(default).endswith(os.path.join(".cache", "sesskit")))
        with mock.patch.dict(
            os.environ,
            {**base, "SESSKIT_CACHE_DIR": "/tmp/x", "CORRAL_CACHE_DIR": "/tmp/z"},
            clear=True,
        ):
            self.assertEqual(str(cache_dir()), "/tmp/x")


def _write_kimi_session(root: Path, workspace: str, session_id: str, cwd: str, text: str) -> Path:
    session_dir = root / workspace / session_id
    wire_dir = session_dir / "agents" / "main"
    wire_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "workDir": cwd,
                "title": "kimi fixture",
                "createdAt": "2026-09-29T00:00:00Z",
                "updatedAt": "2026-09-29T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )
    wire = wire_dir / "wire.jsonl"
    wire.write_text(
        json.dumps(
            {
                "type": "context.append_message",
                "time": 1_758_000_000_000,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return wire


def _scan_kimi(root: Path, live: list, host=None, envs: dict | None = None):
    from sesskit.parsers import kimi

    cache = mock.Mock()
    cache.get_session.return_value = None
    with (
        mock.patch.object(kimi, "SESSIONS_DIR", str(root)),
        mock.patch.object(kimi, "live_processes", return_value=list(live)),
        mock.patch.object(kimi, "process_command_line", return_value="kimi -y"),
        mock.patch.object(
            kimi,
            "process_environ",
            side_effect=lambda pid, **kwargs: dict(envs.get(pid, {}) if envs else {}),
        ),
        mock.patch.object(kimi, "process_start_time", return_value=None),
        mock.patch.object(kimi, "get_cache", return_value=cache),
    ):
        return kimi.scan_sessions(limit=10, host=host)


class KimiHostPlumbingTests(unittest.TestCase):
    SESSION_ID = "session_abc1234567890"

    def test_native_ident_binds_without_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_kimi_session(root, "ws1", self.SESSION_ID, cwd, "hello kimi")
            sessions = _scan_kimi(
                root,
                [(999, os.path.realpath(cwd))],
                envs={999: {"SESSKIT_SESSION_ID": self.SESSION_ID}},
            )
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 999)

    def test_host_ident_binds_with_host_only(self) -> None:
        host = HostExtension(
            name="fake",
            env_keys=("FAKE_KIMI_ID",),
            session_id_from_env=lambda env: str(env.get("FAKE_KIMI_ID") or ""),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_kimi_session(root, "ws1", self.SESSION_ID, cwd, "hello kimi")
            neutral = _scan_kimi(
                root, [(999, os.path.realpath(cwd))], envs={999: {"FAKE_KIMI_ID": self.SESSION_ID}}
            )
            hosted = _scan_kimi(
                root,
                [(999, os.path.realpath(cwd))],
                host=host,
                envs={999: {"FAKE_KIMI_ID": self.SESSION_ID}},
            )
        self.assertFalse(neutral[0]["live"])
        self.assertTrue(hosted[0]["live"])
        self.assertEqual(hosted[0]["pid"], 999)


if __name__ == "__main__":
    unittest.main()
