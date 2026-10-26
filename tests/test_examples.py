"""Runnable consumer recipes: real native wiring, privacy, scope, and failure honesty."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sesskit import get_adapter, list_adapters

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("export_for_search", "usage_and_outcomes", "follow_session")
SID = "1f3c2647-1234-4567-890a-123456789abc"


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def history(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    directory = home / ".pi" / "agent" / "sessions" / "project"
    directory.mkdir(parents=True)
    path = directory / f"2026-10-25T10-00-00-000Z_{SID}.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": SID, "cwd": str(project),
         "timestamp": "2026-10-25T10:00:00Z"},
        {"type": "message", "id": "u1", "parentId": None,
         "timestamp": "2026-10-25T10:00:01Z",
         "message": {"role": "user", "content": "Find the indexing bug"}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "timestamp": "2026-10-25T10:00:02Z",
         "message": {"role": "assistant", "model": "example-model",
                     "usage": {"input": 10, "output": 4, "totalTokens": 14},
                     "content": [{"type": "thinking", "thinking": "Check ordering"},
                                 {"type": "text", "text": "Use a stable key"}],
                     "stopReason": "stop"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", os.defpath), "LANG": "en_US.UTF-8",
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"), "SESSKIT_CACHE_DIR": str(home / ".cache" / "sesskit"),
        "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("SESSKIT_INCLUDE_EPHEMERAL", None)
    session = {"source": "pi", "id": SID, "path": str(path), "mtime": 1792922402.0}
    return path, session, env


def run_example(name, env, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "examples" / f"{name}.py"), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def registry_with(monkeypatch, module, sessions):
    calls = []

    def scan(**kwargs):
        calls.append(kwargs)
        return sessions

    monkeypatch.setattr(module, "default_registry", lambda: SimpleNamespace(
        get=lambda runtime: SimpleNamespace(scan_sessions=scan),
    ))
    return calls


def test_export_real_discovery_and_search_document(history):
    path, _session, env = history
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = run_example("export_for_search", env, "--runtime", "pi", "--limit", "1")
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["id"] == f"pi:{SID}"
    assert row["message_count"] == 2
    assert row["text"] == "user: Find the indexing bug\n\nassistant: Use a stable key"
    assert str(path) not in result.stdout
    assert json.loads(result.stderr)["exported"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_usage_real_discovery_keeps_observations_not_totals(history):
    _path, _session, env = history
    result = run_example("usage_and_outcomes", env, "--runtime", "pi", "--limit", "1")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert (report["scanned"], report["loaded"], report["failed"]) == (1, 1, 0)
    assert report["outcomes"] == {"done": 1}
    row = report["sessions"][0]
    assert row["usage_state"] == "recorded"
    assert row["usage_observations"][0]["usage"]["total_tokens"] == 14
    assert "Find the indexing bug" not in result.stdout
    assert "Use a stable key" not in result.stdout
    assert "total_cost" not in result.stdout


def test_follow_real_discovery_and_no_change(history):
    _path, _session, env = history
    result = run_example("follow_session", env, "--runtime", "pi", "--session-id", SID,
                         "--polls", "2", "--interval", "0")
    assert result.returncode == 0, result.stderr
    first, second = [json.loads(line) for line in result.stdout.splitlines()]
    assert first["reset"] is True
    assert first["events"]
    assert second["reset"] is False
    assert second["events"] == []
    assert second["cursor"] == first["cursor"]
    assert second["generation"] == first["generation"]


def test_follow_append_and_branch_reset(history):
    path, session, _env = history
    stream = example("follow_session").follow(session, polls=3, interval=0)
    first = next(stream)
    user = {"type": "message", "id": "u2", "parentId": "a1",
            "message": {"role": "user", "content": "Continue"}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(user) + "\n")
    appended = next(stream)
    assert appended["reset"] is False
    assert appended["events"][0]["message_id"] == f"pi:{SID}:entry:u2"
    branch = {"type": "message", "id": "branch", "parentId": "u1",
              "message": {"role": "user", "content": "Different direction"}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(branch) + "\n")
    reset = next(stream)
    assert reset["reset"] is True
    assert reset["generation"] != first["generation"]
    assert f"pi:{SID}:entry:u2" not in {event["message_id"] for event in reset["events"]}


@pytest.mark.parametrize("name", SCRIPTS)
def test_bad_arguments_never_scan(name, history):
    _path, _session, env = history
    args = ["--runtime", "pi", "--limit", "0"]
    if name == "follow_session":
        args.extend(["--session-id", SID])
    result = run_example(name, env, *args)
    assert result.returncode == 2
    assert not result.stdout


@pytest.mark.parametrize("interval", ["nan", "inf", "-1"])
def test_follow_rejects_invalid_interval(interval, history):
    _path, _session, env = history
    result = run_example("follow_session", env, "--runtime", "pi", "--session-id", SID,
                         "--interval", interval)
    assert result.returncode == 2
    assert "finite and nonnegative" in result.stderr


def test_follow_rejects_prefix_and_unsupported_runtime(history):
    _path, _session, env = history
    missing = run_example("follow_session", env, "--runtime", "pi", "--session-id", SID[:8])
    assert missing.returncode == 3
    assert json.loads(missing.stderr)["error"] == "session_not_found"
    unsupported = run_example("follow_session", env, "--runtime", "kimi", "--session-id", SID)
    assert unsupported.returncode == 1
    assert json.loads(unsupported.stderr)["error"] == "incremental_unsupported"


@pytest.mark.parametrize("name", ["export_for_search", "usage_and_outcomes"])
def test_zero_discovery_and_archive_scope(name, monkeypatch, capsys):
    module = example(name)
    calls = registry_with(monkeypatch, module, [])
    assert module.main(["--runtime", "pi", "--limit", "7", "--include-missing-cwd"]) == 0
    captured = capsys.readouterr()
    output = json.loads(captured.err if name == "export_for_search" else captured.out)
    assert output["scanned"] == 0
    assert calls == [{"limit": 7, "include_missing_cwd": True}]


@pytest.mark.parametrize("name", ["export_for_search", "usage_and_outcomes"])
def test_missing_history_is_failure_not_empty_success(name, history, monkeypatch, capsys):
    path, session, _env = history
    path.unlink()  # This fixture is owned by this test, never a native user history.
    module = example(name)
    registry_with(monkeypatch, module, [session])
    assert module.main(["--runtime", "pi"]) == 1
    captured = capsys.readouterr()
    assert str(path) not in captured.out + captured.err
    if name == "usage_and_outcomes":
        report = json.loads(captured.out)
        assert report["failed"] == 1
        assert report["loaded"] == 0
    else:
        assert not captured.out
        assert json.loads(captured.err.splitlines()[-1])["failed"] == 1


@pytest.mark.parametrize("name", ["export_for_search", "usage_and_outcomes"])
def test_scan_failure_diagnostic_does_not_leak_exception(name, monkeypatch, capsys):
    module = example(name)

    def fail(**_kwargs):
        raise OSError("private path and secret must not be printed")

    monkeypatch.setattr(module, "default_registry", lambda: SimpleNamespace(
        get=lambda runtime: SimpleNamespace(scan_sessions=fail),
    ))
    assert module.main(["--runtime", "pi"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "scan_failed", "exception_type": "OSError"}


def test_export_partial_failure_keeps_success_and_nonzero_status(history, monkeypatch, capsys):
    _path, session, _env = history
    module = example("export_for_search")
    missing = {**session, "id": "missing", "path": str(ROOT / "absent-example-history.jsonl")}
    registry_with(monkeypatch, module, [session, missing])
    assert module.main(["--runtime", "pi"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["session_id"] == SID
    assert json.loads(captured.err.splitlines()[-1])["exported"] == 1
    assert json.loads(captured.err.splitlines()[-1])["failed"] == 1


def test_usage_preserves_native_zero_and_missing_values(history):
    path, session, _env = history
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["message"]["usage"] = {"input": 0}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    row = example("usage_and_outcomes").inspect_session(session)
    usage = row["usage_observations"][0]["usage"]
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] is None
    assert usage["cost"] is None


def test_usage_supported_without_observations_is_not_unsupported(history):
    path, session, _env = history
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["message"].pop("usage")
    rows[-1]["message"].pop("model")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    row = example("usage_and_outcomes").inspect_session(session)
    assert row["state"] == "available"
    assert row["usage_state"] == "not_recorded"
    assert row["usage_observations"] == []


def test_usage_unsupported_is_not_zero_or_successful_load():
    row = example("usage_and_outcomes").inspect_session({"source": "kimi", "id": "s", "path": ""})
    assert row["state"] == "unsupported"
    assert row["usage_state"] == "unsupported"
    assert row["usage_observations"] == []
    assert row["outcome"] == "unknown"


def test_capability_table_tracks_actual_adapters():
    guide = (ROOT / "docs" / "INTEGRATION_GUIDE.md").read_text(encoding="utf-8")
    fields = ("typed_activity", "incremental_reading", "structured_questions",
              "native_turn_boundaries", "usage_data", "compaction_markers")
    for adapter in list_adapters():
        cells = ["Yes" if getattr(adapter.capabilities, field) else "No" for field in fields]
        assert f"| {adapter.id} | " + " | ".join(cells) + " |" in guide
    assert all(not get_adapter(a.id).capabilities.permission_records for a in list_adapters())


def test_readmes_use_same_published_wheel_and_route_to_examples():
    url = "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"
    for filename in ("README.md", "README.zh-CN.md"):
        content = (ROOT / filename).read_text(encoding="utf-8")
        assert f'python -m pip install "{url}"' in content
        assert "docs/INTEGRATION_GUIDE.md" in content
        assert "issues/new?template=integration.yml" in content
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "integration.yml").is_file()
