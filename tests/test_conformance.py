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
from dataclasses import replace
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
    AnswerRecord,
    Evidence,
    InteractionRequest,
    QuestionItem,
    SessionOutcome,
    ToolResultOutcome,
    Usage,
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


def test_verify_session_kimi_typed_unsupported_keeps_legacy_checks():
    with tempfile.TemporaryDirectory() as directory:
        session = _kimi_fixture(Path(directory))
        result = verify_session(session)
    assert result["state"] == "unsupported"
    assert result["activity_load_ok"] is True
    assert result["event_load_ok"] is True
    assert result["parity_ok"] is None
    assert result["reader"]["comparison_state"] == "unsupported"
    assert result["verification_state"] == "passed"
    assert result["violations"] == []


# --- verify wiring ---


def _fake_registry(sessions: list[dict]) -> ParserRegistry:
    def scan(limit=50, keep_ids=None, cwd_filter=None):
        return list(sessions)[:limit]

    def load(session):
        from sesskit.models import ConversationMessage
        return [ConversationMessage(role="user", text="hi")]

    return ParserRegistry([RuntimeParser(id="pi", display_name="Pi",
                                         _scan=scan, _load=load)])


def test_verify_session_reader_suffix_semantics():
    # Tail-window readers report a suffix with snapshot-global seqs; the
    # gate accepts full equality or exact-suffix equality, and requires
    # backward pages to reassemble the full snapshot.
    for builder in (_claude_fixture, _codex_fixture):
        with tempfile.TemporaryDirectory() as directory:
            session = builder(Path(directory))
            result = verify_session(session)
            assert result["reader"]["supported"] is True
            assert result["reader"]["ok"] is True
            assert result["reader"]["mismatch"] is False
            assert result["reader"]["page_ok"] is True
            assert result["violations"] == []


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


def test_verify_session_missing_history_is_counted_not_raised(tmp_path):
    missing = tmp_path / "missing.jsonl"
    result = verify_session({"source": "pi", "path": str(missing), "id": "missing"})
    assert result["load_ok"] is False
    assert result["load_error_kind"] == "ConversationLoadError"
    assert result["state"] == "unavailable"


def test_cursor_prompt_history_directory_is_a_valid_source(tmp_path):
    prompts = tmp_path / "prompt_history.json"
    prompts.write_text('["prompt"]', encoding="utf-8")
    session = {"source": "cursor", "path": str(tmp_path)}
    before = conformance._source_stamp(session)
    assert conformance._source_exists(session) is True
    prompts.write_text('["prompt", "next"]', encoding="utf-8")
    assert conformance._source_stamp(session) != before


def test_verify_all_passes_on_fixture_registry():
    with tempfile.TemporaryDirectory() as directory:
        session = _pi_fixture(Path(directory))
        registry = _fake_registry([session])
        report = verify_all(registry, ["pi"], sample=10)
    assert report["passed"] is True
    assert report["totals"]["sampled"] == 1
    assert report["totals"]["violations"] == {}
    assert report["runtimes"]["pi"]["timings_ms"]["scan"] is not None


def test_verify_all_mixed_sampled_and_no_history_is_unverified(tmp_path):
    session = _pi_fixture(tmp_path)
    registry = ParserRegistry([
        RuntimeParser(id="pi", display_name="Pi",
                      _scan=lambda limit=50: [session], _load=lambda session: []),
        RuntimeParser(id="codex", display_name="Codex",
                      _scan=lambda limit=50: [], _load=lambda session: []),
    ])
    report = verify_all(registry, ["pi", "codex"], sample=1)
    assert report["passed"] is False
    assert report["verification_state"] == "unverified"
    assert report["runtimes"]["pi"]["verification_state"] == "passed"
    assert report["runtimes"]["codex"]["verification_state"] == "unverified"


def test_verify_all_kimi_legacy_sample_can_pass(tmp_path):
    session = _kimi_fixture(tmp_path)
    registry = ParserRegistry([RuntimeParser(
        id="kimi", display_name="Kimi",
        _scan=lambda limit=50: [session], _load=lambda session: [])])
    report = verify_all(registry, ["kimi"], sample=1)
    assert report["passed"] is True
    assert report["verification_state"] == "passed"
    assert report["runtimes"]["kimi"]["states"] == {"unsupported": 1}


def test_verify_all_scan_failure_cannot_pass():
    def fail_scan(limit=50):
        raise RuntimeError("scan failed")

    registry = ParserRegistry([RuntimeParser(
        id="pi", display_name="Pi", _scan=fail_scan, _load=lambda session: [])])
    report = verify_all(registry, ["pi"], sample=10)
    assert report["passed"] is False
    assert report["verification_state"] == "failed"
    assert report["runtimes"]["pi"]["verification_state"] == "failed"
    assert report["runtimes"]["pi"]["violations"]["scan_failure"] == 1


def test_verify_all_no_samples_is_unverified_not_passed():
    report = verify_all(_fake_registry([]), ["pi"], sample=10)
    assert report["passed"] is False
    assert report["verification_state"] == "unverified"
    assert report["runtimes"]["pi"]["verification_state"] == "unverified"
    assert report["runtimes"]["pi"]["skip_reason"] == "no_sessions"


def test_verify_all_missing_scanned_codex_history_is_failure(tmp_path):
    missing = {"source": "codex", "path": str(tmp_path / "missing-codex.jsonl"),
               "id": "missing"}
    registry = ParserRegistry([RuntimeParser(
        id="codex", display_name="Codex", _scan=lambda limit=50: [missing],
        _load=lambda session: [])])
    report = verify_all(registry, ["codex"], sample=1)
    runtime = report["runtimes"]["codex"]
    assert report["passed"] is False
    assert runtime["load_failed"] == 1
    assert runtime["states"] == {"unavailable": 1}
    assert runtime["verification_state"] == "failed"


def _reader_comparison_snapshot() -> ActivitySnapshot:
    events = _typed_pair_flow()
    events[1] = replace(events[1], usage=Usage(NATIVE, input_tokens=7, output_tokens=2))
    events[2] = replace(
        events[2],
        interaction=InteractionRequest(
            "question", NATIVE, resolution="answered", resolution_evidence=NATIVE,
            request_id="q-1", tool_call_id="t-1",
            questions=(QuestionItem(prompt="Which?", options=()),),
            answers=(AnswerRecord(question_index=0, text="first"),),
        ),
    )
    events[3] = replace(events[3], error=AgentError("provider", "reported", NATIVE))
    return ActivitySnapshot("available", tuple(events), SessionOutcome("done", INFERRED))


def _verify_with_fake_reader(snapshot, poll_events=None, *, poll_state="available",
                             poll_outcome=None, page_events=None,
                             event_load_error=False, reader_error=False,
                             source_changes=False, source_present=True,
                             page_generation="g1", generation_changes_during_pages=False):
    from types import SimpleNamespace

    from sesskit.models import ConversationMessage

    poll_events = tuple(snapshot.events if poll_events is None else poll_events)
    page_events = tuple(snapshot.events if page_events is None else page_events)
    outcome = snapshot.outcome if poll_outcome is None else poll_outcome

    class FakeReader:
        page_count = 0

        def poll(self):
            return SimpleNamespace(events=poll_events, reset=True, generation="g1",
                                   outcome=outcome, state=poll_state)

        def page(self, before=None, limit=200):
            self.page_count += 1
            if generation_changes_during_pages:
                if self.page_count == 1:
                    return SimpleNamespace(events=tuple(snapshot.events[-1:]),
                                           before="older", has_more=True, generation="g1")
                return SimpleNamespace(events=tuple(snapshot.events[:-1]), before=None,
                                       has_more=False, generation="g2")
            return SimpleNamespace(events=page_events, before=None, has_more=False,
                                   generation=page_generation)

    conversation = [ConversationMessage("user", "Run checks."),
                    ConversationMessage("assistant", "Running.")]
    source_stamp = mock.patch.object(conformance, "_source_stamp", return_value=("stable",))
    if source_changes:
        source_stamp = mock.patch.object(
            conformance, "_source_stamp",
            side_effect=[("stable",), ("stable",), ("changed",), ("changed",),
                         ("changed",), ("changed",)],
        )
    with (source_stamp,
          mock.patch.object(conformance, "_source_exists", return_value=source_present),
          mock.patch.object(conformance, "load_session_conversation", return_value=conversation),
          mock.patch.object(conformance, "load_activity", return_value=snapshot),
          mock.patch.object(
              conformance, "load_events",
              side_effect=RuntimeError if event_load_error else None,
              **({} if event_load_error else {"return_value": to_v1_dicts(snapshot)}),
          ),
          mock.patch.object(conformance, "supports_incremental", return_value=True),
          mock.patch.object(
              conformance, "open_activity_reader",
              side_effect=RuntimeError if reader_error else None,
              **({} if reader_error else {"return_value": FakeReader()}),
          )):
        return verify_session({"source": "pi", "path": "unused", "id": "s1"})


def test_event_load_exception_prevents_verification_pass():
    result = _verify_with_fake_reader(_reader_comparison_snapshot(), event_load_error=True)
    assert result["event_load_ok"] is False
    assert result["verification_state"] == "failed"
    assert any(item["rule"] == "event_load" for item in result["violations"])


def test_reader_exception_prevents_verification_pass():
    result = _verify_with_fake_reader(_reader_comparison_snapshot(), reader_error=True)
    assert result["reader"]["ok"] is False
    assert result["verification_state"] == "failed"
    assert any(item["rule"] == "reader_failure" for item in result["violations"])


def test_absent_scanned_source_cannot_pass_even_if_loaders_return_empty():
    empty = ActivitySnapshot("empty", (), SessionOutcome("unknown", UNKNOWN))
    result = _verify_with_fake_reader(empty, source_present=False)
    assert result["verification_state"] == "failed"
    assert any(item["rule"] == "source_missing" for item in result["violations"])


@pytest.mark.parametrize("mutation", [
    "text", "raw_input", "raw_output", "correlation", "error", "question_link",
    "usage", "outcome",
])
def test_reader_comparison_rejects_same_seq_type_with_changed_content(mutation):
    snapshot = _reader_comparison_snapshot()
    altered = list(snapshot.events)
    outcome = None
    if mutation == "text":
        altered[0] = replace(altered[0], text="different")
    elif mutation == "raw_input":
        altered[2] = replace(altered[2], raw_input={"command": "different"})
    elif mutation == "raw_output":
        altered[3] = replace(altered[3], raw_output="different")
    elif mutation == "correlation":
        altered[3] = replace(altered[3], call_id="other-call")
    elif mutation == "error":
        altered[3] = replace(altered[3], error=AgentError("provider", "different", NATIVE))
    elif mutation == "question_link":
        old = altered[2].interaction
        altered[2] = replace(altered[2], interaction=replace(old, tool_call_id="other-call"))
    elif mutation == "usage":
        altered[1] = replace(altered[1], usage=replace(altered[1].usage, input_tokens=8))
    elif mutation == "outcome":
        outcome = SessionOutcome("unknown", UNKNOWN)
    result = _verify_with_fake_reader(snapshot, altered, poll_outcome=outcome)
    assert any(item["rule"] == "reader_snapshot_match" for item in result["violations"])
    assert result["verification_state"] == "failed"


def test_reader_comparison_accepts_exact_global_seq_suffix():
    snapshot = _reader_comparison_snapshot()
    result = _verify_with_fake_reader(snapshot, snapshot.events[2:])
    assert result["reader"]["mismatch"] is False
    assert result["reader"]["page_ok"] is True
    assert result["verification_state"] == "passed"


def test_reader_comparison_rejects_read_state_mismatch():
    snapshot = _reader_comparison_snapshot()
    result = _verify_with_fake_reader(snapshot, poll_state="empty")
    assert any(item["rule"] == "reader_snapshot_match" for item in result["violations"])


def test_reader_page_comparison_checks_complete_event_content():
    snapshot = _reader_comparison_snapshot()
    altered = list(snapshot.events)
    altered[3] = replace(altered[3], raw_output="different")
    result = _verify_with_fake_reader(snapshot, page_events=altered)
    assert any(item["rule"] == "reader_page_match" for item in result["violations"])


def test_live_source_change_makes_cross_view_comparison_inconclusive():
    result = _verify_with_fake_reader(_reader_comparison_snapshot(), source_changes=True)
    assert result["source_stable"] is False
    assert result["comparison_state"] == "inconclusive"
    assert result["verification_state"] == "inconclusive"
    assert result["reader"]["page_ok"] is None
    assert result["violations"] == []


def test_reader_generation_change_during_paging_is_inconclusive():
    result = _verify_with_fake_reader(
        _reader_comparison_snapshot(), generation_changes_during_pages=True)
    assert result["reader"]["mismatch"] is False
    assert result["reader"]["page_ok"] is None
    assert result["verification_state"] == "inconclusive"
    assert result["violations"] == []


def test_conversation_checks_missing_roles_and_empty_event_view():
    from sesskit.models import ConversationMessage

    user = ActivityEvent(1, "user_message", NATIVE, text="hello")
    conversation = [ConversationMessage("user", "hello")]
    assert any(v.rule == "conversation_user_coverage"
               for v in check_conversation(conversation, []))
    assert any(v.rule == "conversation_user_coverage"
               for v in check_conversation([], [user]))


def test_typed_conversation_rejects_substrings_duplicates_and_reordering():
    from sesskit.models import ConversationMessage

    events = [ActivityEvent(1, "user_message", NATIVE, text="hello"),
              ActivityEvent(2, "assistant_message", NATIVE, text="answer")]
    altered_text = [ConversationMessage("user", "well hello there"),
                    ConversationMessage("assistant", "answer")]
    duplicate = [ConversationMessage("user", "hello"),
                 ConversationMessage("user", "hello"),
                 ConversationMessage("assistant", "answer")]
    reordered = [ConversationMessage("assistant", "answer"),
                 ConversationMessage("user", "hello")]
    assert any(v.rule == "conversation_user_coverage"
               for v in check_conversation(altered_text, events))
    assert any(v.rule == "conversation_user_coverage"
               for v in check_conversation(duplicate, events))
    assert any(v.rule == "conversation_order"
               for v in check_conversation(reordered, events))


def test_kimi_legacy_conversation_allows_documented_assistant_chunk_grouping():
    from sesskit.models import ConversationMessage

    events = [
        {"type": "user_message", "seq": 1, "text": "question"},
        {"type": "assistant_message", "seq": 2, "text": "first chunk"},
        {"type": "thinking", "seq": 3, "text": "internal"},
        {"type": "assistant_message", "seq": 4, "text": "second chunk"},
        {"type": "user_message", "seq": 5, "text": "next"},
    ]
    conversation = [ConversationMessage("user", "question"),
                    ConversationMessage("assistant", "first chunk\n\nsecond chunk"),
                    ConversationMessage("user", "next")]
    assert check_conversation(conversation, events) == []


def test_conversation_respects_intentionally_hidden_activity():
    injected = ActivityEvent(1, "user_message", NATIVE, text="injected", origin="injected")
    lifecycle = ActivityEvent(2, "lifecycle", NATIVE, stop_reason="stop")
    assert check_conversation([], [injected, lifecycle]) == []


def test_empty_snapshot_must_match_empty_v1_view():
    snapshot = ActivitySnapshot("empty", (), SessionOutcome("unknown", UNKNOWN))
    mismatch = [{"type": "user_message", "seq": 1, "text": "unexpected"}]
    assert any(v.rule == "v1_parity"
               for v in run_conformance("pi", snapshot=snapshot,
                                        v1_events=mismatch).violations)


def test_probe_exception_is_not_reported_as_unchecked_success():
    with tempfile.TemporaryDirectory() as directory:
        session = _claude_fixture(Path(directory))
        with mock.patch.object(conformance, "open_activity_reader", side_effect=RuntimeError):
            result = conformance.probe_jsonl_reader(session)
    assert result["checked"] is False
    assert result["error_kind"] == "RuntimeError"


def test_probe_source_mutation_during_temp_copy_is_inconclusive():
    with tempfile.TemporaryDirectory() as directory:
        session = _claude_fixture(Path(directory))
        copyfile = conformance.shutil.copyfile

        def copy_then_append_blank_line(source, destination):
            result = copyfile(source, destination)
            with open(source, "ab") as handle:
                handle.write(b"\n")
            return result

        with mock.patch.object(conformance.shutil, "copyfile", side_effect=copy_then_append_blank_line):
            result = conformance.probe_jsonl_reader(session)
    assert result["checked"] is False
    assert result["inconclusive"] is True
    assert result["skip_reason"] == "source_changed_during_copy"
    assert result["error_kind"] is None


def test_verify_runtime_propagates_probe_failure():
    with tempfile.TemporaryDirectory() as directory:
        session = _claude_fixture(Path(directory))
        good_result = verify_session(session)
        assert good_result["verification_state"] == "passed"
        registry = ParserRegistry([RuntimeParser(
            id="claude", display_name="Claude", _scan=lambda limit=50: [session],
            _load=lambda session: [])])
        failed_probe = {"checked": False, "append_parity_ok": None,
                        "nochange_empty_ok": None, "error_kind": "RuntimeError"}
        with (mock.patch.object(conformance, "verify_session", return_value=good_result),
              mock.patch.object(conformance, "probe_jsonl_reader", return_value=failed_probe)):
            report = conformance.verify_runtime(registry, "claude", 1)
    assert report["verification_state"] == "failed"
    assert report["violations"]["reader_append_match"] == 1


def test_verify_cli_exit_codes(capsys):
    with tempfile.TemporaryDirectory() as directory:
        session = _pi_fixture(Path(directory))
        registry = _fake_registry([session])
        with mock.patch.object(cli, "default_registry", lambda: registry):
            code = cli.dispatch(["verify", "--sample", "5", "--compact"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["data"]["passed"] is True

    with mock.patch.object(cli, "default_registry", lambda: _fake_registry([])):
        code = cli.dispatch(["verify", "--runtime", "pi", "--sample", "5", "--compact"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"]["passed"] is False
    assert out["data"]["verification_state"] == "unverified"

    bad_report = {"passed": False, "runtimes": {}, "sample": 1, "totals": {}}
    with mock.patch.object(conformance, "verify_all", lambda *a, **k: bad_report):
        with mock.patch.object(cli, "default_registry", lambda: _fake_registry([])):
            code = cli.dispatch(["verify"])
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["data"]["passed"] is False


def test_verify_cli_zero_skips_scan_and_negative_is_usage_error(capsys):
    calls = 0

    def scan(limit=50):
        nonlocal calls
        calls += 1
        return []

    registry = ParserRegistry([RuntimeParser(
        id="pi", display_name="Pi", _scan=scan, _load=lambda session: [])])
    with mock.patch.object(cli, "default_registry", lambda: registry):
        code = cli.dispatch(["verify", "--runtime", "pi", "--sample", "0", "--json"])
    zero = json.loads(capsys.readouterr().out)
    assert code == 1
    assert calls == 0
    assert zero["data"]["verification_state"] == "unverified"
    assert zero["data"]["runtimes"]["pi"]["skip_reason"] == "sample_limit_zero"

    with mock.patch.object(cli, "default_registry", lambda: registry):
        code = cli.dispatch(["verify", "--runtime", "pi", "--sample", "-1", "--json"])
    negative = json.loads(capsys.readouterr().out)
    assert code == 2
    assert negative["ok"] is False
    assert negative["error"]["code"] == "usage_error"
    assert calls == 0
