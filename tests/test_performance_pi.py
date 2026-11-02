"""Pi performance projection: collector, views, adapter wiring.

Collector/view tests run without the core performance module. Tests that
need ``sesskit.performance`` skip until the core worker's module lands
(dependency_not_ready).
"""

from __future__ import annotations

import json

import pytest

from sesskit.performance_adapters import pi as pi_perf


def _entry(entry_id, message, timestamp="2026-10-21T00:00:01.000Z"):
    entry = {"type": "message", "timestamp": timestamp, "message": message}
    if entry_id is not None:
        entry["id"] = entry_id
    return entry


def _assistant(stop="stop", duration=2000, output=52, ts=1761000000000, response="resp-1", model="m", provider="p"):
    message = {
        "role": "assistant",
        "stopReason": stop,
        "timestamp": ts,
        "model": model,
        "provider": provider,
        "responseId": response,
    }
    if duration != "absent":
        message["durationMs"] = duration
    if output != "absent":
        message["usage"] = {"input": 10, "output": output}
    return message


def _rows(entries, start=1):
    return [(position, entry) for position, entry in zip(range(start, start + len(entries)), entries)]


def test_successful_stop_input_is_rate_eligible():
    inputs, skipped = pi_perf.collect_pi_performance_inputs(_rows([_entry("e1", _assistant())]))
    assert skipped == []
    assert len(inputs) == 1
    item = inputs[0]
    assert item["record"] == "entry:e1"
    assert item["kind"] == "response_elapsed"
    assert item["scope_kind"] == "message"
    assert item["duration_ms"] == 2000.0
    assert item["output_tokens"] == 52
    assert item["output_semantics"] == "inclusive_output"
    assert item["allow_rate"] is True
    assert item["started_at"] == 1761000000000 / 1000.0
    assert item["ended_at"] is None
    assert item["message_id"] == "e1"
    assert item["response_id"] == "resp-1"
    assert item["model"] == "m"
    assert item["provider"] == "p"
    assert item["clock"] == "mixed"
    assert item["diagnostics"] == ()
    # Exact arithmetic the core builder authorizes: output*1000/duration.
    assert 52 * 1000 / item["duration_ms"] == pytest.approx(26.0)


def test_tooluse_nonzero_output_is_eligible():
    inputs, _ = pi_perf.collect_pi_performance_inputs(
        _rows([_entry("e2", _assistant(stop="toolUse", duration=500, output=512))])
    )
    assert len(inputs) == 1
    assert inputs[0]["allow_rate"] is True
    assert inputs[0]["output_tokens"] == 512
    assert 512 * 1000 / inputs[0]["duration_ms"] == pytest.approx(1024.0)


def test_error_zero_output_is_untrusted_placeholder():
    inputs, skipped = pi_perf.collect_pi_performance_inputs(
        _rows([_entry("e3", _assistant(stop="error", duration=1500, output=0))])
    )
    assert skipped == []
    assert len(inputs) == 1
    item = inputs[0]
    assert item["duration_ms"] == 1500.0
    assert item["output_tokens"] is None
    assert item["output_semantics"] == "unknown"
    assert item["allow_rate"] is False
    assert any("output-untrusted" in text for text in item["diagnostics"])


def test_aborted_output_is_untrusted_even_when_nonzero():
    inputs, _ = pi_perf.collect_pi_performance_inputs(
        _rows([_entry("e4", _assistant(stop="aborted", duration=800, output=37))])
    )
    assert inputs[0]["output_tokens"] is None
    assert inputs[0]["allow_rate"] is False


def test_missing_duration_is_skipped_legacy_and_deferred():
    entries = [
        _entry("legacy", _assistant(duration="absent")),
        _entry("deferred", _assistant(duration="absent", stop="stop")),
    ]
    inputs, skipped = pi_perf.collect_pi_performance_inputs(_rows(entries))
    assert inputs == []
    assert [item["record"] for item in skipped] == ["entry:legacy", "entry:deferred"]
    assert all(item["reason"] == "duration-missing-or-invalid" for item in skipped)


@pytest.mark.parametrize("bad", [True, False, 0, -5, "2000", None, float("nan"), float("inf"), 7.5 - 7.5 - 1.0])
def test_invalid_durations_are_skipped(bad):
    inputs, skipped = pi_perf.collect_pi_performance_inputs(_rows([_entry("bad", _assistant(duration=bad))]))
    assert inputs == []
    assert len(skipped) == 1


def test_zero_and_negative_outputs_rejected_by_validator():
    assert pi_perf.valid_output_tokens(0) == 0
    assert pi_perf.valid_output_tokens(-1) is None
    assert pi_perf.valid_output_tokens(True) is None
    assert pi_perf.valid_output_tokens(3.0) == 3
    assert pi_perf.valid_output_tokens(3.5) is None


def test_success_without_usage_keeps_timing_without_rate():
    inputs, _ = pi_perf.collect_pi_performance_inputs(_rows([_entry("nou", _assistant(output="absent"))]))
    assert len(inputs) == 1
    assert inputs[0]["output_tokens"] is None
    assert inputs[0]["allow_rate"] is False


def test_toolresult_auxiliary_timing_is_ignored():
    entry = _entry("t1", {"role": "toolResult", "durationMs": 900, "timestamp": 1761000000000})
    inputs, skipped = pi_perf.collect_pi_performance_inputs(_rows([entry]))
    assert inputs == [] and skipped == []


def test_record_locator_keeps_physical_positions():
    rows = [(1, _entry(None, _assistant())), (5, _entry(None, _assistant()))]
    inputs, _ = pi_perf.collect_pi_performance_inputs(rows)
    assert [item["record"] for item in inputs] == ["entry:#1", "entry:#5"]


def test_entry_append_time_is_never_an_endpoint():
    entry = _entry("e9", _assistant(), timestamp="2026-10-21T00:05:00.000Z")
    inputs, _ = pi_perf.collect_pi_performance_inputs(_rows([entry]))
    assert inputs[0]["ended_at"] is None
    assert inputs[0]["started_at"] == 1761000000000 / 1000.0


def test_active_branch_keeps_positions_and_rejects_bad_view():
    main = _entry("m1", _assistant())
    fork = _entry("f1", _assistant())
    fork["parentId"] = "m1"
    leaf = _entry("leaf", _assistant())
    leaf["parentId"] = "f1"
    side = _entry("side", _assistant())
    side["parentId"] = "m1"
    entries = [main, fork, leaf, side]
    rows = pi_perf.select_rows(entries, [1, 2, 3, 4], history_view="active_branch")
    # Official loader walks back from the file's last record: side is the leaf.
    assert [position for position, _ in rows] == [1, 4]
    rows_all = pi_perf.select_rows(entries, [1, 2, 3, 4], history_view="all_branches")
    assert [position for position, _ in rows_all] == [1, 2, 3, 4]
    # View validation needs no core module.
    with pytest.raises(ValueError):
        pi_perf.select_rows(entries, [1, 2, 3, 4], history_view="bogus")
    with pytest.raises(ValueError):
        pi_perf.load({"path": "/nonexistent"}, history_view="bogus")


def test_public_core_is_present_and_missing_history_is_unavailable(tmp_path):
    from sesskit import load_performance

    assert pi_perf.get_capabilities().supported is True
    assert load_performance({"source": "pi", "id": "s",
                             "path": str(tmp_path / "missing.jsonl")}).state == "unavailable"


perf = pytest.importorskip("sesskit.performance", reason="dependency_not_ready")


def _write_session(path, entries):
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry) + "\n" for entry in entries)


def test_capabilities_shape():
    capabilities = pi_perf.get_capabilities()
    assert capabilities.runtime == "pi"
    assert capabilities.supported is True
    assert "response_elapsed" in capabilities.timing_kinds
    assert "response_elapsed" in capabilities.rate_kinds
    assert set(capabilities.history_views) == {"all_branches", "active_branch"}
    assert capabilities.provider_decode_timing is False


def test_load_fixture_end_to_end(tmp_path):
    entries = [
        {"type": "session", "id": "s1"},
        _entry("e1", _assistant(stop="stop", duration=2000, output=52)),
        _entry("e2", _assistant(stop="error", duration=1500, output=0)),
        _entry("e3", _assistant(duration="absent")),
    ]
    history = tmp_path / "s.jsonl"
    _write_session(history, entries)
    snapshot = pi_perf.load({"source": "pi", "id": "s1", "path": str(history)})
    assert snapshot.state == "available"
    assert snapshot.coverage == "partial"
    assert snapshot.session_ref == "pi:s1"
    assert snapshot.entries_read == len(entries)
    assert len(snapshot.records) == 2
    rated = [record for record in snapshot.records if record.tokens_per_second]
    assert len(rated) == 1
    assert rated[0].output_tokens == 52
    assert rated[0].tokens_per_second == pytest.approx(26.0)
    assert rated[0].rate_basis == "derived"
    unrated = [record for record in snapshot.records if not record.tokens_per_second]
    assert unrated[0].output_tokens is None


def test_load_empty_and_unreadable(tmp_path):
    history = tmp_path / "empty.jsonl"
    _write_session(history, [{"type": "session", "id": "s9"}])
    snapshot = pi_perf.load({"source": "pi", "id": "s9", "path": str(history)})
    assert snapshot.state == "empty"
    assert snapshot.coverage == "not_recorded"
    assert snapshot.records == ()
    missing = pi_perf.load({"source": "pi", "id": "gone", "path": str(tmp_path / "gone.jsonl")})
    assert missing.state == "unavailable"


def test_adapter_delegates_to_pi_module(tmp_path):
    from sesskit.adapters.pi import PiAdapter

    history = tmp_path / "a.jsonl"
    _write_session(history, [_entry("e1", _assistant())])
    adapter = PiAdapter()
    assert adapter.performance_capabilities().runtime == "pi"
    snapshot = adapter.load_performance({"source": "pi", "id": "a", "path": str(history)})
    assert snapshot.state == "available"
    assert snapshot.records[0].message_id == "e1"
