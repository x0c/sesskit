"""Kimi performance adapter tests (locked contract sesskit.performance/v1).

Client-observed step timing (``step.end``) and wall-clock turn durations
(``turn.ended``): rated client throughput only on successful steps with a
trustworthy native count and positive stream duration; explicit no-rate
records otherwise, never zero speed.
"""

from __future__ import annotations

import json
import tempfile
import unittest

import pytest

performance = pytest.importorskip("sesskit.performance")
perf_kimi = pytest.importorskip("sesskit.performance_adapters.kimi")


def _success_step(**overrides):
    event = {
        "type": "step.end",
        "uuid": "af259483-step-2",
        "turnId": "0",
        "step": 2,
        "finishReason": "end_turn",
        "usage": {"inputOther": 15, "output": 2, "inputCacheRead": 10, "inputCacheCreation": 0},
        "llmFirstTokenLatencyMs": 48514,
        "llmStreamDurationMs": 664,
        "llmRequestBuildMs": 2,
        "llmServerFirstTokenMs": 48512,
        "llmServerDecodeMs": 506,
        "llmClientConsumeMs": 158,
        "llmClientBlockedMs": 466,
        "messageId": "chatcmpl-test1",
        "providerFinishReason": "completed",
        "rawFinishReason": "stop",
    }
    event.update(overrides)
    return {"type": "context.append_loop_event", "event": event}


def _error_step(**overrides):
    event = {"type": "step.end", "uuid": "err-uuid-1", "turnId": "0", "step": 1, "finishReason": "error"}
    event.update(overrides)
    return {"type": "context.append_loop_event", "event": event}


def _turn_ended(**overrides):
    entry = {"type": "turn.ended", "turnId": "0", "durationMs": 346536, "reason": "completed", "time": 1790772283338}
    entry.update(overrides)
    return entry


def _write_wire(tmpdir, lines):
    path = f"{tmpdir}/wire.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in lines)
    return path


def _session(path, session_id="s1"):
    return {"source": "kimi", "id": session_id, "path": path}


def _by_kind(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record.kind, []).append(record)
    return grouped


class CapabilitiesTests(unittest.TestCase):
    def test_kimi_declares_client_observed_support(self) -> None:
        capabilities = perf_kimi.CAPABILITIES
        self.assertEqual(capabilities.runtime, "kimi")
        self.assertTrue(capabilities.supported)
        self.assertIn("stream_elapsed", capabilities.timing_kinds)
        self.assertIn("first_part_latency", capabilities.timing_kinds)
        self.assertIn("turn_elapsed", capabilities.timing_kinds)
        self.assertIn("native_duration", capabilities.timing_kinds)
        self.assertEqual(tuple(capabilities.rate_kinds), ("stream_elapsed",))
        self.assertFalse(capabilities.provider_decode_timing)
        self.assertFalse(capabilities.incremental_support)

    def test_adapter_exposes_capabilities(self) -> None:
        from sesskit.adapters.kimi import KimiAdapter

        self.assertIs(KimiAdapter().performance_capabilities(), perf_kimi.CAPABILITIES)


class StreamRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _load(self, entries, session_id="s1"):
        lines = [json.dumps(entry) for entry in entries]
        path = _write_wire(self.tmp.name, lines)
        return perf_kimi.load(_session(path, session_id))

    def test_success_step_yields_rated_client_throughput(self) -> None:
        snapshot = self._load([_success_step(), _turn_ended()])
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(snapshot.coverage, "partial")
        kinds = _by_kind(snapshot.records)
        stream = kinds["stream_elapsed"]
        self.assertEqual(len(stream), 1)
        record = stream[0]
        self.assertEqual(record.duration_ms, 664)
        self.assertEqual(record.output_tokens, 2)
        self.assertEqual(record.output_semantics, "provider_output")
        self.assertAlmostEqual(record.tokens_per_second, 2000 / 664)
        self.assertEqual(record.rate_basis, "derived")
        self.assertEqual(record.turn_id, "0")
        self.assertEqual(record.message_id, "chatcmpl-test1")
        self.assertEqual(record.scope_kind, "turn")
        self.assertIn("client_observed_throughput_not_provider_decode", record.diagnostics)
        fields = [evidence.field for evidence in record.evidence]
        self.assertIn("usage.output", fields)
        self.assertIn("llmStreamDurationMs", fields)
        self.assertTrue(any(evidence.origin == "inferred" for evidence in record.evidence))

    def test_first_part_latency_has_no_rate(self) -> None:
        snapshot = self._load([_success_step()])
        kinds = _by_kind(snapshot.records)
        self.assertEqual(len(kinds["first_part_latency"]), 1)
        record = kinds["first_part_latency"][0]
        self.assertEqual(record.duration_ms, 48514)
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertIn("client_first_part_latency_not_provider_ttft", record.diagnostics)

    def test_native_durations_never_claim_provider_decode(self) -> None:
        snapshot = self._load([_success_step()])
        kinds = _by_kind(snapshot.records)
        self.assertEqual(len(kinds["native_duration"]), 5)
        by_field = {}
        for record in kinds["native_duration"]:
            self.assertIsNone(record.tokens_per_second)
            self.assertEqual(record.rate_basis, "unavailable")
            native_fields = [evidence.field for evidence in record.evidence if evidence.origin == "native"]
            self.assertEqual(len(native_fields), 1)
            by_field[native_fields[0]] = record
        self.assertEqual(by_field["llmServerDecodeMs"].duration_ms, 506)
        for field in ("llmServerDecodeMs", "llmServerFirstTokenMs"):
            self.assertIn("client_observed_not_provider_decode", by_field[field].diagnostics)

    def test_turn_elapsed_is_wall_duration_never_rated(self) -> None:
        snapshot = self._load([_turn_ended()])
        kinds = _by_kind(snapshot.records)
        self.assertEqual(len(kinds["turn_elapsed"]), 1)
        record = kinds["turn_elapsed"][0]
        self.assertEqual(record.duration_ms, 346536)
        self.assertEqual(record.clock, "wall")
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertIn("turn_duration_not_generation_speed", record.diagnostics)

    def test_error_step_has_explicit_no_rate_never_zero(self) -> None:
        snapshot = self._load([_error_step()])
        kinds = _by_kind(snapshot.records)
        self.assertEqual(list(kinds), ["step_elapsed"])
        record = kinds["step_elapsed"][0]
        self.assertIsNone(record.duration_ms)
        self.assertIsNone(record.output_tokens)
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertIn("error_finish_no_rate", record.diagnostics)

    def test_standalone_usage_record_is_never_joined(self) -> None:
        entries = [
            {"type": "llm.request", "turnStep": "0.1", "time": 1790771987000},
            {
                "type": "usage.record",
                "agentId": "main",
                "model": "m",
                "time": 1790772283338,
                "usageScope": "turn",
                "usage": {"inputOther": 15, "output": 2, "inputCacheRead": 10, "inputCacheCreation": 0},
            },
        ]
        snapshot = self._load(entries)
        self.assertEqual(snapshot.state, "empty")
        self.assertEqual(snapshot.coverage, "not_recorded")
        self.assertEqual(snapshot.records, ())

    def test_zero_stream_duration_has_no_rate(self) -> None:
        # Core validation degrades a zero divisor to missing: no
        # stream_elapsed record, explicit no-rate everywhere, never zero.
        event = _success_step()
        event["event"]["llmStreamDurationMs"] = 0
        snapshot = self._load([event])
        kinds = _by_kind(snapshot.records)
        self.assertNotIn("stream_elapsed", kinds)
        for record in snapshot.records:
            self.assertIsNone(record.tokens_per_second)
            self.assertEqual(record.rate_basis, "unavailable")
        all_diagnostics = [diagnostic for record in snapshot.records for diagnostic in record.diagnostics]
        self.assertIn("nonpositive_stream_duration_no_rate", all_diagnostics)

    def test_invalid_values_rejected_without_crash(self) -> None:
        event = _success_step()
        event["event"]["usage"] = {"inputOther": 0, "output": True, "inputCacheRead": 0, "inputCacheCreation": 0}
        event["event"]["llmStreamDurationMs"] = float("nan")
        event["event"]["llmClientConsumeMs"] = -5
        snapshot = self._load([event])
        kinds = _by_kind(snapshot.records)
        self.assertNotIn("stream_elapsed", kinds)
        # Four surviving extras; the negative one is dropped, never kept.
        self.assertEqual(len(kinds["native_duration"]), 4)
        self.assertEqual(len(kinds["first_part_latency"]), 1)
        for record in snapshot.records:
            self.assertIsNone(record.tokens_per_second)
            self.assertEqual(record.rate_basis, "unavailable")
        all_diagnostics = [diagnostic for record in snapshot.records for diagnostic in record.diagnostics]
        self.assertIn("untrusted_usage_output", all_diagnostics)
        self.assertIn("untrusted_stream_duration_ms", all_diagnostics)
        self.assertIn("untrusted_llmClientConsumeMs", all_diagnostics)

    def test_bool_and_infinity_rejected(self) -> None:
        event = _success_step()
        event["event"]["usage"]["output"] = 1
        event["event"]["llmStreamDurationMs"] = float("inf")
        event["event"]["llmFirstTokenLatencyMs"] = True
        snapshot = self._load([event])
        kinds = _by_kind(snapshot.records)
        self.assertNotIn("stream_elapsed", kinds)
        self.assertNotIn("first_part_latency", kinds)
        # Valid extras survive; the valid count has no rate without duration.
        self.assertEqual(len(kinds["native_duration"]), 5)
        for record in snapshot.records:
            self.assertIsNone(record.tokens_per_second)
            self.assertEqual(record.rate_basis, "unavailable")
        all_diagnostics = [diagnostic for record in snapshot.records for diagnostic in record.diagnostics]
        self.assertIn("untrusted_stream_duration_ms", all_diagnostics)
        self.assertIn("untrusted_first_part_latency_ms", all_diagnostics)

    def test_multiple_steps_have_stable_distinct_ids(self) -> None:
        first = self._load([_success_step(), _turn_ended()], session_id="s9")
        second = self._load([_success_step(), _turn_ended()], session_id="s9")
        first_ids = [record.performance_id for record in first.records]
        second_ids = [record.performance_id for record in second.records]
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(set(first_ids)), len(first_ids))
        other = self._load([_success_step()], session_id="s9")
        self.assertEqual(len(other.records), len(first.records) - 1)


class LoadBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unreadable_path_is_unavailable(self) -> None:
        snapshot = perf_kimi.load(_session(f"{self.tmp.name}/missing.jsonl"))
        self.assertEqual(snapshot.state, "unavailable")
        self.assertEqual(snapshot.records, ())
        self.assertIsNone(snapshot.watermark)
        self.assertIn("history_unreadable", snapshot.diagnostics)

    def test_empty_file_is_empty_not_recorded(self) -> None:
        path = _write_wire(self.tmp.name, [])
        snapshot = perf_kimi.load(_session(path))
        self.assertEqual(snapshot.state, "empty")
        self.assertEqual(snapshot.coverage, "not_recorded")
        self.assertIn("no_native_timing_records", snapshot.diagnostics)

    def test_malformed_lines_skipped_with_count(self) -> None:
        lines = [json.dumps(_success_step()), "not json{{{", "[1, 2]", json.dumps(_turn_ended())]
        path = _write_wire(self.tmp.name, lines)
        snapshot = perf_kimi.load(_session(path))
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(snapshot.entries_read, 4)
        self.assertIn("skipped_malformed:1", snapshot.diagnostics)
        self.assertIn("skipped_non_object:1", snapshot.diagnostics)

    def test_unsupported_view_raises(self) -> None:
        path = _write_wire(self.tmp.name, [json.dumps(_success_step())])
        with self.assertRaises(ValueError):
            perf_kimi.load(_session(path), history_view="active_branch")

    def test_snapshot_serializes_to_safe_dict(self) -> None:
        path = _write_wire(self.tmp.name, [json.dumps(_success_step())])
        snapshot = perf_kimi.load(_session(path))
        payload = performance.performance_snapshot_to_dict(snapshot)
        self.assertEqual(payload["schema"], performance.PERFORMANCE_SCHEMA_ID)
        self.assertEqual(payload["runtime"], "kimi")
        self.assertNotIn("path", json.dumps(payload))

    def test_adapter_load_performance_wires_session(self) -> None:
        from sesskit.adapters.kimi import KimiAdapter

        path = _write_wire(self.tmp.name, [json.dumps(_success_step())])
        snapshot = KimiAdapter().load_performance(_session(path))
        self.assertEqual(snapshot.state, "available")
        self.assertEqual(snapshot.session_ref, "kimi:s1")


if __name__ == "__main__":
    unittest.main()
