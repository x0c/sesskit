"""Conformance suite: every runtime adapter passes the same invariants.

Typed runtimes (pi, claude, codex, cursor, opencode) are exercised through
their real loaders on synthetic native-history fixtures: snapshot load,
v1 parity, and plain-conversation consistency. Kimi is covered on the
v1/load paths only; its typed adaptation is deferred.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from sesskit import cli, conformance
from sesskit.activity import load_activity, to_v1_dicts
from sesskit.conformance import (
    check_conversation,
    check_outcome,
    check_pairing,
    check_seq,
    check_v1_parity,
    run_conformance,
    verify_all,
    verify_session,
)
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    AgentError,
    Evidence,
    SessionOutcome,
    ToolResultOutcome,
)
from sesskit.parsers import kimi as kimi_parser
from sesskit.registry import ParserRegistry, RuntimeParser, load_session_conversation
from sesskit.transcript import load_events

NATIVE = Evidence("native", record="r1")
INFERRED = Evidence("inferred", field="tail_role", record="r1")
UNKNOWN = Evidence("unknown")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def _typed_pair_flow() -> list[ActivityEvent]:
    return [
        ActivityEvent(1, "user_message", NATIVE, text="Run checks."),
        ActivityEvent(2, "assistant_message", NATIVE, text="Running."),
        ActivityEvent(3, "tool_call", NATIVE, name="bash", call_id="t-1",
                      raw_input={"command": "true"}),
        ActivityEvent(4, "tool_result", NATIVE, call_id="t-1", raw_output="ok",
                      result=ToolResultOutcome("ok", INFERRED)),
    ]


def _done_snapshot(events: list[ActivityEvent] | None = None) -> ActivitySnapshot:
    events = events if events is not None else _typed_pair_flow()
    return ActivitySnapshot("available", tuple(events),
                            SessionOutcome("done", INFERRED))


# --- synthetic typed snapshots, parametrized over typed runtimes ---


@pytest.mark.parametrize("runtime", ["pi", "claude", "codex", "cursor", "opencode"])
def test_typed_snapshot_passes_conformance(runtime):
    from sesskit.models import ConversationMessage

    snapshot = _done_snapshot()
    v1 = to_v1_dicts(snapshot)
    conversation = [
        ConversationMessage("user", "Run checks."),
        ConversationMessage("assistant", "Running."),
    ]
    report = run_conformance(runtime, snapshot=snapshot, v1_events=v1,
                             conversation=conversation)
    assert report.passed, report.violations
    assert report.event_count == 4


def test_kimi_v1_only_path():
    from sesskit.models import ConversationMessage

    v1 = [
        {"type": "user_message", "seq": 1, "ts": 1.0, "text": "hello"},
        {"type": "tool_call", "seq": 2, "ts": 1.0, "id": "t1",
         "name": "Read", "kind": "read", "input": {}},
        {"type": "tool_result", "seq": 3, "ts": 1.0, "call_id": "t1",
         "status": "ok", "output": "done"},
        {"type": "assistant_message", "seq": 4, "ts": 1.0, "text": "read it"},
    ]
    conversation = [ConversationMessage("user", "hello"),
                    ConversationMessage("assistant", "read it")]
    report = run_conformance("kimi", snapshot=None, v1_events=v1,
                             conversation=conversation)
    assert report.passed, report.violations


# --- negative cases: each invariant fires ---


def test_seq_must_strictly_increase():
    events = _typed_pair_flow()
    bad = list(events)
    bad[2] = ActivityEvent(2, "tool_call", NATIVE, name="bash", call_id="t-1",
                           raw_input={})
    assert any(v.rule == "seq_order" for v in check_seq(bad))


def test_unpaired_tool_result_is_flagged():
    events = _typed_pair_flow()
    orphan = ActivityEvent(5, "tool_result", NATIVE, call_id="ghost",
                           raw_output="x",
                           result=ToolResultOutcome("ok", INFERRED))
    assert any(v.rule == "tool_pairing"
               for v in check_pairing(list(events) + [orphan]))


def test_done_with_unmatched_calls_stays_unknown():
    events = [e for e in _typed_pair_flow() if e.type != "tool_result"]
    snapshot = ActivitySnapshot("available", tuple(events), SessionOutcome("done", INFERRED))
    assert any(v.rule == "unknown_stays_unknown" for v in check_outcome(snapshot))


def test_done_ignores_stale_unmatched_call_from_earlier_turn():
    # A native terminal marker closes its turn: an unmatched call left in
    # an earlier turn must not block a later clean completion (live Codex
    # histories close earlier turns with task_complete).
    events = [
        ActivityEvent(1, "tool_call", NATIVE, name="exec", call_id="stale",
                      raw_input={}, turn_id="turn-1"),
        ActivityEvent(2, "user_message", NATIVE, text="Next.", turn_id="turn-2"),
        ActivityEvent(3, "tool_call", NATIVE, name="bash", call_id="t-1",
                      raw_input={"command": "true"}, turn_id="turn-2"),
        ActivityEvent(4, "tool_result", NATIVE, call_id="t-1", raw_output="ok",
                      result=ToolResultOutcome("ok", INFERRED), turn_id="turn-2"),
        ActivityEvent(5, "assistant_message", NATIVE, text="Done.",
                      turn_id="turn-2"),
    ]
    snapshot = ActivitySnapshot("available", tuple(events), SessionOutcome("done", NATIVE))
    assert not [v for v in check_outcome(snapshot)
                if v.rule == "unknown_stays_unknown"]


def test_done_still_flags_unmatched_call_in_terminal_turn():
    events = [
        ActivityEvent(1, "tool_call", NATIVE, name="bash", call_id="t-1",
                      raw_input={"command": "true"}, turn_id="turn-2"),
        ActivityEvent(2, "assistant_message", NATIVE, text="Done.",
                      turn_id="turn-2"),
    ]
    snapshot = ActivitySnapshot("available", tuple(events), SessionOutcome("done", NATIVE))
    assert any(v.rule == "unknown_stays_unknown" for v in check_outcome(snapshot))


def test_done_must_not_carry_error():
    snapshot = ActivitySnapshot(
        "available", tuple(_typed_pair_flow()),
        SessionOutcome("aborted", NATIVE,
                       error=AgentError("provider", "boom", NATIVE)),
    )
    assert not [v for v in check_outcome(snapshot) if v.rule == "done_has_no_error"]
    # A done outcome with an error cannot even be constructed; the guard stays.
    with pytest.raises(ValueError):
        SessionOutcome("done", INFERRED, error=AgentError("x", "y", NATIVE))


def test_aborted_without_error_is_flagged():
    snapshot = ActivitySnapshot("available", tuple(_typed_pair_flow()),
                                SessionOutcome("aborted", NATIVE))
    assert any(v.rule == "aborted_has_error" for v in check_outcome(snapshot))


def test_done_on_user_tail_is_flagged():
    events = [ActivityEvent(1, "user_message", NATIVE, text="Hello?")]
    snapshot = ActivitySnapshot("available", tuple(events), SessionOutcome("done", INFERRED))
    assert any(v.rule == "outcome_tail" for v in check_outcome(snapshot))


def test_native_terminal_marker_done_is_not_upgraded():
    # A native completion record with no text emits no event, leaving a
    # user tail; the native evidence still supports done.
    events = [ActivityEvent(1, "user_message", NATIVE, text="Hello?")]
    snapshot = ActivitySnapshot("available", tuple(events), SessionOutcome("done", NATIVE))
    assert not [v for v in check_outcome(snapshot) if v.rule == "outcome_tail"]


def test_v1_parity_mismatch_reports_first_diff():
    snapshot = _done_snapshot()
    v1 = to_v1_dicts(snapshot)
    v1 = [dict(e) for e in v1]
    v1[1] = {**v1[1], "text": "tampered"}
    violations = check_v1_parity(snapshot, v1)
    assert len(violations) == 1 and violations[0].rule == "v1_parity"
    assert "seq=2" in violations[0].detail


def test_conversation_user_coverage():
    from sesskit.models import ConversationMessage

    events = _typed_pair_flow()
    conversation = [ConversationMessage("user", "something never said")]
    violations = check_conversation(conversation, events)
    assert any(v.rule == "conversation_user_coverage" for v in violations)


def test_violation_details_carry_no_content():
    snapshot = _done_snapshot()
    v1 = [dict(e) for e in to_v1_dicts(snapshot)]
    v1[0] = {**v1[0], "text": "secret content here"}
    report = run_conformance("pi", snapshot=snapshot, v1_events=v1)
    assert not report.passed
    for violation in report.violations:
        assert "secret" not in violation.detail


# --- loader-based fixtures: real adapters on synthetic native history ---


def _pi_fixture(directory: Path) -> dict:
    path = directory / "pi.jsonl"
    _write_jsonl(path, [
        {"type": "session", "id": "pi-1", "timestamp": "2026-09-01T00:00:00Z",
         "cwd": "/tmp/pi-conformance"},
        {"type": "message", "id": "u1", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": "Run checks."}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Running."},
             {"type": "toolCall", "id": "t-1", "name": "bash",
              "arguments": {"command": "true"}}]}},
        {"type": "message", "id": "tr1", "parentId": "a1",
         "timestamp": "2026-09-01T00:00:03Z",
         "message": {"role": "toolResult", "content": "ok",
                     "toolCallId": "t-1", "isError": False}},
    ])
    return {"source": "pi", "path": str(path), "id": "pi-1"}


def _claude_fixture(directory: Path) -> dict:
    path = directory / "claude.jsonl"
    _write_jsonl(path, [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "origin": {"kind": "human"},
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "Run checks."}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Running."},
             {"type": "tool_use", "id": "t-1", "name": "Bash",
              "input": {"command": "true"}}]}},
        {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t-1",
              "content": "ok", "is_error": False}]}},
    ])
    return {"source": "claude", "path": str(path), "id": "claude-1"}


def _codex_fixture(directory: Path) -> dict:
    path = directory / "codex.jsonl"
    _write_jsonl(path, [
        {"type": "response_item", "timestamp": "2026-09-01T00:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Run checks."}]}},
        {"type": "response_item", "timestamp": "2026-09-01T00:00:02Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Running."}]}},
        {"type": "response_item", "timestamp": "2026-09-01T00:00:03Z",
         "payload": {"type": "function_call", "call_id": "t-1", "name": "shell",
                     "arguments": json.dumps({"cmd": "true"})}},
        {"type": "response_item", "timestamp": "2026-09-01T00:00:04Z",
         "payload": {"type": "function_call_output", "call_id": "t-1",
                     "output": "ok"}},
    ])
    return {"source": "codex", "path": str(path), "id": "codex-1"}


def _cursor_fixture(directory: Path) -> dict:
    path = directory / "store.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    objects = [
        {"role": "user", "content": "Run checks."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Running."},
            {"type": "tool-call", "toolCallId": "t-1", "toolName": "bash",
             "args": {"command": "true"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": "t-1", "result": "ok"},
        ]},
    ]
    for index, obj in enumerate(objects):
        conn.execute("INSERT INTO blobs VALUES (?, ?)",
                     (f"blob-{index:04d}", json.dumps(obj).encode()))
    conn.commit()
    conn.close()
    return {"source": "cursor", "path": str(path), "id": "cursor-1"}


def _opencode_fixture(directory: Path) -> dict:
    path = directory / "opencode.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
            session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session VALUES ('s1')")
    conn.execute("INSERT INTO message VALUES ('m1', 's1', 1000, ?)",
                 (json.dumps({"role": "user", "time": {"created": 1000}}),))
    conn.execute("INSERT INTO message VALUES ('m2', 's1', 2000, ?)",
                 (json.dumps({"role": "assistant", "finish": "stop",
                              "time": {"created": 2000}}),))
    parts = [
        ("p1", "m1", 1000, {"type": "text", "text": "Run checks."}),
        ("p2", "m2", 2000, {"type": "text", "text": "Running."}),
        ("p3", "m2", 2001, {"type": "tool", "tool": "bash", "callID": "t-1",
                             "state": {"status": "completed",
                                       "input": {"command": "true"},
                                       "output": "ok"}}),
    ]
    for part_id, message_id, stamp, data in parts:
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                     (part_id, message_id, "s1", stamp, json.dumps(data)))
    conn.commit()
    conn.close()
    return {"source": "opencode", "path": str(path), "id": "s1"}


def _kimi_fixture(directory: Path) -> dict:
    path = directory / "wire.jsonl"
    _write_jsonl(path, [
        {"type": "context.append_message", "time": 1_784_275_205_000,
         "message": {"role": "user", "origin": {"kind": "user"},
                     "content": [{"type": "text", "text": "Run checks."}]}},
        {"type": "context.append_loop_event", "time": 1_784_275_206_000,
         "event": {"type": "tool.call", "toolCallId": "t-1", "name": "Read",
                   "args": {"path": "/tmp/guide.md"}}},
        {"type": "context.append_loop_event", "time": 1_784_275_207_000,
         "event": {"type": "tool.result", "toolCallId": "t-1",
                   "result": {"output": "guide body"}}},
        {"type": "context.append_loop_event", "time": 1_784_275_208_000,
         "event": {"type": "content.part",
                   "part": {"type": "text", "text": "Checked."}}},
    ])
    return {"source": "kimi", "path": str(path), "id": "kimi-1"}


@pytest.mark.parametrize("runtime", ["pi", "claude", "codex", "cursor", "opencode"])
def test_loader_fixtures_pass_conformance(runtime):
    builders = {"pi": _pi_fixture, "claude": _claude_fixture,
                "codex": _codex_fixture, "cursor": _cursor_fixture,
                "opencode": _opencode_fixture}
    with tempfile.TemporaryDirectory() as directory:
        session = builders[runtime](Path(directory))
        snapshot = load_activity(session)
        assert snapshot.state == "available", snapshot.state
        v1 = load_events(session)
        assert v1, "fixture must yield v1 events"
        conversation = load_session_conversation(session)
        assert conversation, "fixture must yield conversation turns"
        report = run_conformance(runtime, snapshot=snapshot, v1_events=v1,
                                 conversation=conversation)
        assert report.passed, report.violations
        assert report.event_count == len(snapshot.events)


def test_kimi_fixture_passes_v1_and_load_paths():
    with tempfile.TemporaryDirectory() as directory:
        session = _kimi_fixture(Path(directory))
        assert load_activity(session).state == "unsupported"
        v1 = load_events(session)
        assert [e["type"] for e in v1] == [
            "user_message", "tool_call", "tool_result", "assistant_message"]
        conversation = kimi_parser.load_conversation(str(Path(directory) / "wire.jsonl"))
        assert [m.role for m in conversation] == ["user", "assistant"]
        report = run_conformance("kimi", snapshot=None, v1_events=v1,
                                 conversation=conversation)
        assert report.passed, report.violations


# --- verify wiring ---


def _fake_registry(sessions: list[dict]) -> ParserRegistry:
    def scan(limit=50, keep_ids=None, cwd_filter=None):
        return list(sessions)[:limit]

    def load(session):
        from sesskit.models import ConversationMessage
        return [ConversationMessage(role="user", text="hi")]

    return ParserRegistry([RuntimeParser(id="pi", display_name="Pi",
                                         _scan=scan, _load=load)])


def test_verify_session_counts_only_no_paths():
    with tempfile.TemporaryDirectory() as directory:
        session = _pi_fixture(Path(directory))
        result = verify_session(session)
    assert result["load_ok"] is True
    assert result["parity_ok"] is True
    assert result["violations"] == []
    assert result["reader"]["supported"] is True
    blob = json.dumps(result)
    assert str(directory) not in blob


def test_verify_session_missing_history_is_counted_not_raised():
    result = verify_session({"source": "pi", "path": "/no/such/file.jsonl",
                             "id": "missing"})
    assert result["load_ok"] is False
    assert result["load_error_kind"] == "ConversationLoadError"
    assert result["state"] == "unavailable"


def test_verify_all_passes_on_fixture_registry():
    with tempfile.TemporaryDirectory() as directory:
        session = _pi_fixture(Path(directory))
        registry = _fake_registry([session])
        report = verify_all(registry, ["pi"], sample=10)
    assert report["passed"] is True
    assert report["totals"]["sampled"] == 1
    assert report["totals"]["violations"] == {}
    assert report["runtimes"]["pi"]["timings_ms"]["scan"] is not None


def test_verify_cli_exit_codes(capsys):
    with tempfile.TemporaryDirectory() as directory:
        session = _pi_fixture(Path(directory))
        registry = _fake_registry([session])
        with mock.patch.object(cli, "default_registry", lambda: registry):
            code = cli.dispatch(["verify", "--sample", "5", "--compact"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["data"]["passed"] is True

    bad_report = {"passed": False, "runtimes": {}, "sample": 1, "totals": {}}
    with mock.patch.object(conformance, "verify_all", lambda *a, **k: bad_report):
        with mock.patch.object(cli, "default_registry", lambda: _fake_registry([])):
            code = cli.dispatch(["verify"])
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["data"]["passed"] is False
