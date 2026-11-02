"""OpenCode historical performance projection tests (fixtures only).

Covers v1/v2 table families, the stale-token failed/retried pairing rule,
tool-calls success, malformed/nonfinite rows, missing tables/columns and
single-connection reads. Requires the shared core module
(``sesskit.performance``); without it this module fails at import with the
reported ``dependency_not_ready`` cause instead of inventing passes.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sesskit.performance_adapters import opencode as perf_opencode


def _write_v1_db(path, *, session_id="ses_1", messages=()):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, "
                 "title TEXT, time_created INTEGER, time_updated INTEGER, "
                 "parent_id TEXT, time_archived INTEGER)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                 "time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                 (session_id, "/work", "t", 100_000, 200_000, None, None))
    for index, (message_id, data) in enumerate(messages):
        payload = data if isinstance(data, str) else json.dumps(data)
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     (message_id, session_id, 100_000 + index, 200_000, payload))
    conn.commit()
    conn.close()


def _write_v2_db(path, *, session_id="ses_1", messages=()):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                 "title TEXT, time_created INTEGER, time_updated INTEGER, "
                 "parent_id TEXT, time_archived INTEGER)")
    conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                 "session_id TEXT, type TEXT, seq INTEGER, "
                 "time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                 (session_id, "/work", "t", 100_000, 200_000, None, None))
    for index, (message_id, row_type, data) in enumerate(messages):
        payload = data if isinstance(data, str) else json.dumps(data)
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                     (message_id, session_id, row_type, index + 1,
                      100_000 + index, 200_000, payload))
    conn.commit()
    conn.close()


def _assistant_data(finish="stop", *, created=1_700_000_000_000,
                    streamed=1_700_000_010_000, completed=1_700_000_012_000,
                    output=200, reasoning=50, error=None, retry=None,
                    model="gpt-5", response_id="resp_1"):
    time = {"created": created}
    if streamed is not None:
        time["streamed"] = streamed
    if completed is not None:
        time["completed"] = completed
    data = {
        "finish": finish,
        "time": time,
        "tokens": {"input": 10, "output": output, "reasoning": reasoning,
                   "cache": {"read": 0, "write": 0}},
        "cost": 0.001,
        "model": {"id": model},
        "agent": "coder",
        "native": {"responseId": response_id},
    }
    if error is not None:
        data["error"] = error
    if retry is not None:
        data["retry"] = retry
    return data


def _records_by_kind(snapshot):
    grouped = {}
    for record in snapshot.records:
        grouped.setdefault(record.kind, []).append(record)
    return grouped


def test_capabilities_declare_no_decode_timing():
    capabilities = perf_opencode.CAPABILITIES
    assert capabilities.runtime == "opencode"
    assert capabilities.supported is True
    assert capabilities.timing_kinds == ("provider_phase_elapsed", "step_elapsed")
    assert capabilities.rate_kinds == ("provider_phase_elapsed", "step_elapsed")
    assert capabilities.history_views == ("all_branches",)
    assert capabilities.provider_decode_timing is False
    assert capabilities.incremental_support is False


def test_v2_success_yields_two_paired_rates(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_user", "user", {"text": "hi"}),
        ("m_asst", "assistant", _assistant_data()),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert snapshot.coverage == "partial"
    assert snapshot.entries_read == 2
    assert snapshot.observed_operations == 1
    assert snapshot.watermark is not None
    assert str(db) not in (snapshot.watermark or "")
    kinds = _records_by_kind(snapshot)
    assert set(kinds) == {"provider_phase_elapsed", "step_elapsed"}
    provider = kinds["provider_phase_elapsed"][0]
    step = kinds["step_elapsed"][0]
    assert provider.duration_ms == pytest.approx(10_000.0)
    assert step.duration_ms == pytest.approx(12_000.0)
    assert provider.output_tokens == 200
    assert provider.output_semantics == "visible_output"
    assert provider.tokens_per_second == pytest.approx(200 * 1000 / 10_000.0)
    assert step.tokens_per_second == pytest.approx(200 * 1000 / 12_000.0)
    assert provider.rate_basis == "derived"
    assert provider.clock == "wall"
    assert provider.started_at == pytest.approx(1_700_000_000.0)
    assert provider.ended_at == pytest.approx(1_700_000_010.0)
    assert provider.message_id == "m_asst"
    assert provider.response_id == "resp_1"
    assert provider.model == "gpt-5"
    assert provider.performance_id != step.performance_id
    origins = {item.origin for item in provider.evidence}
    assert "native" in origins and "inferred" in origins
    assert any(item.field == "tokens_per_second" for item in provider.evidence)
    assert "visible_output_excludes_reasoning" in snapshot.diagnostics


def test_v2_tool_calls_success_yields_rates(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_asst", "assistant", _assistant_data(finish="tool-calls")),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    kinds = _records_by_kind(snapshot)
    assert set(kinds) == {"provider_phase_elapsed", "step_elapsed"}
    for record in snapshot.records:
        assert record.tokens_per_second is not None
        assert record.rate_basis == "derived"


def test_v2_error_row_preserves_timing_without_rate(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_bad", "assistant", _assistant_data(
            finish="error",
            error={"type": "APIError", "message": "boom", "status": 429})),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert snapshot.entries_read == 1
    kinds = _records_by_kind(snapshot)
    assert kinds["provider_phase_elapsed"][0].duration_ms == pytest.approx(10_000.0)
    assert kinds["step_elapsed"][0].duration_ms == pytest.approx(12_000.0)
    for record in snapshot.records:
        assert record.tokens_per_second is None
        assert record.rate_basis == "unavailable"
        assert record.output_tokens == 200
    assert any("error_present_no_rate" in text
               for text in snapshot.diagnostics)


def test_v2_retry_marker_row_never_yields_rate(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_retry", "assistant", _assistant_data(
            retry={"attempt": 1, "at": 1_700_000_005_000})),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert snapshot.records, "retry timing must be preserved, not dropped"
    for record in snapshot.records:
        assert record.tokens_per_second is None
        assert record.rate_basis == "unavailable"
    assert any("retry_present_no_rate" in text
               for text in snapshot.diagnostics)


def test_v2_inflight_row_preserves_created_without_rate(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_live", "assistant", _assistant_data(
            finish="tool-calls", streamed=None, completed=None)),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    kinds = _records_by_kind(snapshot)
    assert set(kinds) == {"provider_phase_elapsed", "step_elapsed"}
    for record in snapshot.records:
        assert record.tokens_per_second is None
        assert record.rate_basis == "unavailable"
        assert record.duration_ms is None
        assert record.started_at == pytest.approx(1_700_000_000.0)
        assert record.ended_at is None
    assert any("missing_timing_no_rate" in text for text in snapshot.diagnostics)


def test_v1_success_yields_step_only(tmp_path):
    db = tmp_path / "opencode.db"
    data = {
        "role": "assistant",
        "finish": "stop",
        "time": {"created": 1_700_000_000_000, "completed": 1_700_000_012_000},
        "tokens": {"input": 5, "output": 120, "reasoning": 30,
                   "cache": {"read": 1, "write": 2}},
        "cost": 0.002,
        "modelID": "gpt-5-mini",
    }
    _write_v1_db(db, messages=[("m_1", data)])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert [record.kind for record in snapshot.records] == ["step_elapsed"]
    record = snapshot.records[0]
    assert record.duration_ms == pytest.approx(12_000.0)
    assert record.tokens_per_second == pytest.approx(120 * 1000 / 12_000.0)
    assert record.output_semantics == "visible_output"
    assert record.model == "gpt-5-mini"
    assert record.response_id is None
    assert any(text == "layout_v1_message" for text in snapshot.diagnostics)


def test_malformed_and_nonfinite_rows_do_not_crash(tmp_path):
    db = tmp_path / "opencode.db"
    good = ("m_good", "assistant", _assistant_data())
    broken = ("m_broken", "assistant", "{not json")
    nan_payload = json.dumps({"finish": "stop",
                              "time": {"created": float("nan"),
                                       "completed": 1_700_000_012_000},
                              "tokens": {"output": 10}})
    _write_v2_db(db, messages=[good, broken, ("m_nan", "assistant", nan_payload),
                               ("m_list", "assistant", "[1,2]")])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert snapshot.entries_read == 4
    assert any("malformed_rows_skipped:2" in text
               for text in snapshot.diagnostics)
    nan_records = [record for record in snapshot.records
                   if record.message_id == "m_nan"]
    assert nan_records, "nonfinite timing must be preserved, not dropped"
    for record in nan_records:
        assert record.tokens_per_second is None
        assert record.output_tokens == 10


def test_negative_and_zero_durations_never_yield_rate(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[
        ("m_neg", "assistant", _assistant_data(
            created=1_700_000_010_000, streamed=1_700_000_005_000,
            completed=1_700_000_012_000)),
        ("m_zero", "assistant", _assistant_data(
            created=1_700_000_010_000, streamed=1_700_000_010_000,
            completed=1_700_000_010_000)),
    ])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    by_message: dict[str, list] = {}
    for record in snapshot.records:
        by_message.setdefault(record.message_id or "", []).append(record)
    neg = {record.kind: record for record in by_message["m_neg"]}
    assert neg["provider_phase_elapsed"].tokens_per_second is None
    assert neg["provider_phase_elapsed"].duration_ms is None
    assert neg["step_elapsed"].tokens_per_second is not None
    for record in by_message["m_zero"]:
        assert record.tokens_per_second is None


def test_missing_tables_columns_and_session_are_unavailable(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(empty), "id": "ses_1"})
    assert snapshot.state == "unavailable"
    assert snapshot.records == ()
    assert "unsupported_layout_missing_tables" in snapshot.diagnostics

    broken = tmp_path / "broken.db"
    conn = sqlite3.connect(str(broken))
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT)")
    conn.execute("INSERT INTO session VALUES (?)", ("ses_1",))
    conn.commit()
    conn.close()
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(broken), "id": "ses_1"})
    assert snapshot.state == "unavailable"

    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[("m_1", "assistant", _assistant_data())])
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_missing"})
    assert snapshot.state == "unavailable"
    assert "session_not_found" in snapshot.diagnostics

    snapshot = perf_opencode.load({"source": "opencode", "id": "ses_1"})
    assert snapshot.state == "unavailable"


def test_single_connection_read_transaction(tmp_path, monkeypatch):
    from sesskit.parsers import opencode as parser

    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[("m_1", "assistant", _assistant_data())])
    calls = []
    real_connect = parser.connect_ro

    def counting_connect(path):
        calls.append(path)
        return real_connect(path)

    monkeypatch.setattr(parser, "connect_ro", counting_connect)
    snapshot = perf_opencode.load(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert len(calls) == 1


def test_unsupported_history_view_fails(tmp_path):
    db = tmp_path / "opencode.db"
    _write_v2_db(db)
    with pytest.raises(ValueError, match="history view"):
        perf_opencode.load({"source": "opencode", "path": str(db), "id": "ses_1"},
                           history_view="active_branch")


def test_adapter_hunk_dispatches_to_owned_module(tmp_path):
    from sesskit.adapters.opencode import OpenCodeAdapter

    adapter = OpenCodeAdapter()
    assert adapter.capabilities.performance_data is True
    assert adapter.performance_capabilities() is perf_opencode.CAPABILITIES
    db = tmp_path / "opencode.db"
    _write_v2_db(db, messages=[("m_1", "assistant", _assistant_data())])
    snapshot = adapter.load_performance(
        {"source": "opencode", "path": str(db), "id": "ses_1"})
    assert snapshot.state == "available"
    assert snapshot.runtime == "opencode"


def test_parse_assistant_timing_rejects_bad_numbers():
    parsed = perf_opencode.parse_assistant_timing(
        {"time": {"created": True, "completed": "x"},
         "tokens": {"output": -1, "reasoning": 1.5},
         "modelID": "  ", "native": {"responseId": 42}})
    assert parsed is not None
    assert parsed["created_ms"] is None
    assert parsed["completed_ms"] is None
    assert parsed["output"] is None
    assert parsed["reasoning"] is None
    assert parsed["model"] is None
    assert parsed["response_id"] is None
    assert perf_opencode.parse_assistant_timing([]) is None
