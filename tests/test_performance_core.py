"""Core performance contract: build validation, identity, feed, summary."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from sesskit.models import Evidence
from sesskit.performance import (
    PERFORMANCE_SCHEMA_ID,
    PerformanceCapabilities,
    PerformanceRecord,
    PerformanceSnapshot,
    load_performance,
    make_performance_record,
    performance_capabilities,
    performance_record_to_dict,
    performance_snapshot_to_dict,
    performance_summary,
    unsupported_performance_snapshot,
    validate_performance_view,
)
from sesskit.performance_io import NativePerformanceRows, read_jsonl_snapshot


def _record(**overrides):
    params = {
        "runtime": "pi",
        "session_ref": "pi:session-1",
        "record": "line:7",
        "kind": "response_elapsed",
        "duration_ms": 1000.0,
        "output_tokens": 2000,
        "allow_rate": True,
        "evidence": (Evidence("native", field="durationMs", record="line:7"),),
    }
    params.update(overrides)
    return make_performance_record(**params)


class BuildValidationTests(unittest.TestCase):
    def test_valid_build_derives_rate_with_quotient_evidence(self) -> None:
        record = _record()
        self.assertEqual(record.tokens_per_second, 2000.0)
        self.assertEqual(record.rate_basis, "derived")
        origins = [item.origin for item in record.evidence]
        self.assertIn("native", origins)
        quotient = [item for item in record.evidence
                    if item.field == "tokens_per_second"]
        self.assertEqual(len(quotient), 1)
        self.assertEqual(quotient[0].origin, "inferred")
        self.assertEqual(record.performance_id,
                         "pi:session-1:line:7:response_elapsed")

    def test_rate_requires_explicit_authorization(self) -> None:
        record = _record(allow_rate=False)
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertEqual(record.duration_ms, 1000.0)
        self.assertEqual(record.output_tokens, 2000)
        self.assertNotIn("rate_unavailable", record.diagnostics)

    def test_bool_count_is_not_a_number(self) -> None:
        record = _record(output_tokens=True)
        self.assertIsNone(record.output_tokens)
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertIn("output_tokens_invalid", record.diagnostics)
        self.assertIn("rate_unavailable", record.diagnostics)
        self.assertEqual(record.record, "line:7")

    def test_negative_count_degrades(self) -> None:
        record = _record(output_tokens=-5)
        self.assertIsNone(record.output_tokens)
        self.assertIsNone(record.tokens_per_second)
        self.assertIn("output_tokens_invalid", record.diagnostics)

    def test_non_integral_float_count_degrades(self) -> None:
        record = _record(output_tokens=3.5)
        self.assertIsNone(record.output_tokens)
        self.assertIn("output_tokens_invalid", record.diagnostics)

    def test_integral_float_count_accepted(self) -> None:
        record = _record(output_tokens=2000.0)
        self.assertEqual(record.output_tokens, 2000)
        self.assertEqual(record.tokens_per_second, 2000.0)

    def test_zero_duration_never_divides(self) -> None:
        record = _record(duration_ms=0)
        self.assertIsNone(record.duration_ms)
        self.assertIsNone(record.tokens_per_second)
        self.assertIn("duration_invalid", record.diagnostics)

    def test_negative_duration_degrades(self) -> None:
        record = _record(duration_ms=-100.0)
        self.assertIsNone(record.duration_ms)
        self.assertIn("duration_invalid", record.diagnostics)

    def test_nonfinite_durations_degrade(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                record = _record(duration_ms=bad)
                self.assertIsNone(record.duration_ms)
                self.assertIsNone(record.tokens_per_second)
                self.assertIn("duration_invalid", record.diagnostics)

    def test_bool_duration_degrades(self) -> None:
        record = _record(duration_ms=True)
        self.assertIsNone(record.duration_ms)
        self.assertIn("duration_invalid", record.diagnostics)

    def test_missing_inputs_stay_missing_with_diagnostic(self) -> None:
        record = _record(duration_ms=None, output_tokens=None)
        self.assertIsNone(record.duration_ms)
        self.assertIsNone(record.output_tokens)
        self.assertIsNone(record.tokens_per_second)
        self.assertEqual(record.rate_basis, "unavailable")
        self.assertIn("rate_unavailable", record.diagnostics)
        self.assertEqual(record.record, "line:7")

    def test_native_zero_rate_stays_zero(self) -> None:
        record = _record(output_tokens=0)
        self.assertEqual(record.output_tokens, 0)
        self.assertEqual(record.tokens_per_second, 0.0)
        self.assertEqual(record.rate_basis, "derived")
        self.assertNotIn("output_tokens_invalid", record.diagnostics)

    def test_invalid_timestamps_degrade(self) -> None:
        record = _record(started_at=float("nan"), ended_at=-3)
        self.assertIsNone(record.started_at)
        self.assertIsNone(record.ended_at)
        self.assertIn("started_at_invalid", record.diagnostics)
        self.assertIn("ended_at_invalid", record.diagnostics)
        self.assertIsNotNone(record.tokens_per_second)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _record(kind="decode_speed")
        with self.assertRaises(ValueError):
            _record(output_semantics="estimated")
        with self.assertRaises(ValueError):
            _record(clock="quantum")

    def test_blank_scope_kind_falls_back(self) -> None:
        record = _record(scope_kind="  ")
        self.assertEqual(record.scope_kind, "unknown")
        self.assertIn("scope_kind_unknown", record.diagnostics)

    def test_all_named_kinds_accepted(self) -> None:
        kinds = (
            "response_elapsed", "provider_phase_elapsed", "step_elapsed",
            "stream_elapsed", "turn_elapsed", "first_output_latency",
            "first_part_latency", "request_to_reply_elapsed",
            "thinking_elapsed", "session_elapsed", "session_api_elapsed",
            "session_api_without_retries_elapsed", "session_tool_elapsed",
            "native_duration",
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertEqual(_record(kind=kind).kind, kind)


class IdentityTests(unittest.TestCase):
    def test_disjoint_sessions_stay_disjoint(self) -> None:
        first = _record()
        other = _record(session_ref="pi:session-2")
        self.assertNotEqual(first.performance_id, other.performance_id)

    def test_same_locator_repeat_stable(self) -> None:
        self.assertEqual(_record().performance_id, _record().performance_id)

    def test_locator_and_kind_shape_identity(self) -> None:
        first = _record()
        moved = _record(record="line:8")
        rekinded = _record(kind="turn_elapsed")
        self.assertNotEqual(first.performance_id, moved.performance_id)
        self.assertNotEqual(first.performance_id, rekinded.performance_id)
        self.assertIn("line:7", first.performance_id)

    def test_missing_session_ref_falls_back_to_runtime(self) -> None:
        record = _record(session_ref=None)
        self.assertIsNone(record.session_ref)
        self.assertTrue(record.performance_id.startswith("pi:"))


class DataclassTests(unittest.TestCase):
    def test_direct_construction_rejects_bad_kind(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceRecord(
                performance_id="x", runtime="pi", record="line:1",
                scope_kind="message", kind="bogus", duration_ms=5.0)

    def test_available_snapshot_requires_records(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceSnapshot("available", "pi", "pi:s1")
        snapshot = PerformanceSnapshot("available", "pi", "pi:s1",
                                       records=(_record(),),
                                       observed_operations=1,
                                       coverage="partial")
        self.assertEqual(len(snapshot.records), 1)

    def test_empty_snapshot_rejects_records(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceSnapshot("empty", "pi", "pi:s1",
                                records=(_record(),))

    def test_snapshot_rejects_bad_coverage(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceSnapshot("empty", "pi", "pi:s1", coverage="complete")

    def test_snapshot_rejects_negative_counts(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceSnapshot("empty", "pi", "pi:s1", entries_read=-1)

    def test_capabilities_reject_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            PerformanceCapabilities("pi", True, timing_kinds=("bogus",))
        capabilities = PerformanceCapabilities(
            "pi", True, timing_kinds=("response_elapsed",),
            rate_kinds=("response_elapsed",),
            history_views=("all_branches", "active_branch"))
        self.assertTrue(capabilities.supported)


class ViewAndDispatchTests(unittest.TestCase):
    def test_default_capabilities_are_unsupported(self) -> None:
        capabilities = performance_capabilities("cursor")
        self.assertFalse(capabilities.supported)
        self.assertEqual(capabilities.timing_kinds, ())
        self.assertEqual(capabilities.rate_kinds, ())

    def test_unknown_runtime_reports_unsupported(self) -> None:
        capabilities = performance_capabilities("bogus-runtime")
        self.assertFalse(capabilities.supported)

    def test_validate_view_accepts_supported(self) -> None:
        capabilities = PerformanceCapabilities("pi", True,
                                               history_views=("all_branches",))
        self.assertIsNone(validate_performance_view("all_branches",
                                                    capabilities))

    def test_validate_view_rejects_unsupported(self) -> None:
        capabilities = PerformanceCapabilities("pi", True,
                                               history_views=("all_branches",))
        with self.assertRaises(ValueError):
            validate_performance_view("active_branch", capabilities)
        with self.assertRaises(ValueError):
            validate_performance_view("bogus", capabilities)

    def test_unsupported_snapshot_shape(self) -> None:
        snapshot = unsupported_performance_snapshot(
            runtime="cursor", session_ref="cursor:s", history_view="all_branches")
        self.assertEqual(snapshot.state, "unsupported")
        self.assertEqual(snapshot.coverage, "unsupported")
        self.assertEqual(snapshot.records, ())
        self.assertIn("runtime_performance_unsupported", snapshot.diagnostics)

    def test_load_unknown_runtime_is_unsupported(self) -> None:
        snapshot = load_performance({"source": "bogus-runtime", "id": "s"})
        self.assertEqual(snapshot.state, "unsupported")
        self.assertEqual(snapshot.coverage, "unsupported")

    def test_load_dispatches_to_adapter(self) -> None:
        expected = PerformanceSnapshot("empty", "fake", "fake:s",
                                       history_view="all_branches")
        adapter = mock.Mock()
        adapter.performance_capabilities.return_value = PerformanceCapabilities(
            "fake", True)
        adapter.load_performance.return_value = expected
        with mock.patch("sesskit.adapters.get_adapter",
                        return_value=adapter):
            snapshot = load_performance({"source": "fake", "id": "s"})
        self.assertIs(snapshot, expected)
        adapter.load_performance.assert_called_once_with(
            {"source": "fake", "id": "s"}, history_view="all_branches")

    def test_load_rejects_unsupported_view(self) -> None:
        adapter = mock.Mock()
        adapter.performance_capabilities.return_value = PerformanceCapabilities(
            "fake", True, history_views=("all_branches",))
        with mock.patch("sesskit.adapters.get_adapter",
                         return_value=adapter), self.assertRaises(ValueError):
            load_performance({"source": "fake", "id": "s"},
                             history_view="active_branch")
        adapter.load_performance.assert_not_called()

    def test_base_adapter_defaults_are_unsupported(self) -> None:
        from sesskit.adapters.cursor import adapter

        capabilities = adapter.performance_capabilities()
        self.assertFalse(capabilities.supported)
        snapshot = adapter.load_performance({"source": "cursor", "id": "s"})
        self.assertEqual(snapshot.state, "unsupported")
        with self.assertRaises(ValueError):
            adapter.load_performance({"source": "cursor", "id": "s"},
                                     history_view="side_branch")


class SerializationTests(unittest.TestCase):
    def test_schema_id(self) -> None:
        self.assertEqual(PERFORMANCE_SCHEMA_ID, "sesskit.performance/v1")

    def test_record_dict_carries_no_payload(self) -> None:
        payload = performance_record_to_dict(_record())
        self.assertEqual(payload["performance_id"],
                         "pi:session-1:line:7:response_elapsed")
        self.assertEqual(payload["scope_kind"], "unknown")
        self.assertIsNone(payload["scope_id"])
        self.assertIsNone(payload["message_id"])
        self.assertIsNone(payload["turn_id"])
        self.assertIsNone(payload["response_id"])
        self.assertEqual(payload["evidence"][0],
                         {"origin": "native", "field": "durationMs",
                          "record": "line:7"})
        blob = repr(payload)
        for leaked in ("prompt", "content", "sk-", "timestamp"):
            self.assertNotIn(leaked, blob)

    def test_record_dict_matches_flat_schema_shape(self) -> None:
        record = _record(message_id="m-1", turn_id="t-1", response_id="r-1",
                         model="model-x", provider="prov", scope_id="s-1",
                         scope_kind="message", started_at=100.0, ended_at=101.0)
        payload = performance_record_to_dict(record)
        for key in ("performance_id", "runtime", "record",
                    "scope_kind", "scope_id", "kind", "duration_ms",
                    "output_tokens", "output_semantics", "tokens_per_second",
                    "rate_basis", "clock", "started_at", "ended_at",
                    "message_id", "turn_id", "response_id", "model",
                    "provider", "evidence", "diagnostics"):
            self.assertIn(key, payload)
        self.assertNotIn("session_ref", payload)
        self.assertNotIn("correlation", payload)
        self.assertNotIn("scope", payload)
        self.assertEqual(payload["message_id"], "m-1")
        self.assertEqual(payload["turn_id"], "t-1")
        self.assertEqual(payload["response_id"], "r-1")

    def test_summary_bounded_and_latest_last(self) -> None:
        first = _record(kind="turn_elapsed", record="line:5",
                        duration_ms=500.0, output_tokens=100,
                        message_id="m-5")
        second = _record(record="line:9")
        snapshot = PerformanceSnapshot(
            "available", "pi", "pi:session-1", records=(first, second),
            observed_operations=2, coverage="partial")
        summary = performance_summary(snapshot)
        self.assertEqual(summary["coverage"], "partial")
        self.assertEqual(summary["record_count"], 2)
        self.assertEqual(summary["rate_count"], 2)
        self.assertEqual(summary["observed_operations"], 2)
        self.assertEqual(summary["kinds"],
                         ["turn_elapsed", "response_elapsed"])
        self.assertLessEqual(len(summary["kinds"]), 25)
        self.assertEqual(summary["latest"]["record"], "line:9")
        self.assertNotIn("average", summary)
        self.assertNotIn("total_duration_ms", summary)

    def test_summary_empty_has_null_latest(self) -> None:
        snapshot = PerformanceSnapshot("empty", "pi", "pi:s")
        summary = performance_summary(snapshot)
        self.assertEqual(summary["record_count"], 0)
        self.assertEqual(summary["rate_count"], 0)
        self.assertIsNone(summary["latest"])
        self.assertEqual(summary["kinds"], [])

    def test_snapshot_dict_hides_watermark(self) -> None:
        snapshot = PerformanceSnapshot(
            "available", "pi", "pi:s", records=(_record(),),
            entries_read=7, watermark="opaque-fingerprint",
            observed_operations=1, coverage="partial",
            diagnostics=("subset",))
        payload = performance_snapshot_to_dict(snapshot)
        self.assertEqual(payload["schema"], PERFORMANCE_SCHEMA_ID)
        self.assertIs(payload["watermark"], True)
        self.assertEqual(payload["entries_read"], 7)
        self.assertEqual(payload["diagnostics"], ["subset"])
        self.assertIn("summary", payload)
        bare = performance_snapshot_to_dict(
            PerformanceSnapshot("empty", "pi", "pi:s"))
        self.assertIs(bare["watermark"], False)


class FeedTests(unittest.TestCase):
    def write_file(self, directory: str, name: str, data: bytes) -> str:
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_unreadable_path_raises_never_empty_success(self) -> None:
        with self.assertRaises(OSError):
            read_jsonl_snapshot("/nonexistent-dir/missing.jsonl")

    def test_complete_file_reads_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_file(directory, "s.jsonl",
                                   b'{"a": 1}\n{"b": 2}\n')
            first = read_jsonl_snapshot(path)
            self.assertEqual([line for line, _ in first.rows], [1, 2])
            self.assertEqual(first.entries_read, 2)
            self.assertEqual(first.diagnostics, ())
            self.assertTrue(first.watermark.strip())
            self.assertNotIn(directory, first.watermark)
            second = read_jsonl_snapshot(path)
            self.assertEqual(second.watermark, first.watermark)

    def test_trailing_partial_line_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_file(directory, "s.jsonl",
                                   b'{"a": 1}\n{"b": 2')
            snapshot = read_jsonl_snapshot(path)
            self.assertEqual([line for line, _ in snapshot.rows], [1])
            self.assertEqual(snapshot.entries_read, 1)
            self.assertIn("trailing_partial_held", snapshot.diagnostics)
            with open(path, "ab") as handle:
                handle.write(b'}\n')
            completed = read_jsonl_snapshot(path)
            self.assertEqual([line for line, _ in completed.rows], [1, 2])
            self.assertEqual(completed.entries_read, 2)
            self.assertNotIn("trailing_partial_held",
                             completed.diagnostics)
            self.assertNotEqual(completed.watermark, snapshot.watermark)

    def test_skipped_lines_counted_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "s3cr3t-payload-marker"
            path = self.write_file(
                directory, "s.jsonl",
                ('{"a": 1}\n'
                 'not json at all\n'
                 '[1, 2]\n'
                 f'{{"leak": "{secret}"}}\n').encode())
            snapshot = read_jsonl_snapshot(path)
            self.assertEqual([line for line, _ in snapshot.rows], [1, 4])
            self.assertEqual(snapshot.entries_read, 4)
            self.assertIn("skipped_malformed:1", snapshot.diagnostics)
            self.assertIn("skipped_non_object:1", snapshot.diagnostics)
            for diagnostic in snapshot.diagnostics:
                self.assertNotIn(secret, diagnostic)

    def test_empty_file_is_empty_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_file(directory, "s.jsonl", b"")
            snapshot = read_jsonl_snapshot(path)
            self.assertEqual(snapshot.rows, ())
            self.assertEqual(snapshot.entries_read, 0)
            self.assertTrue(snapshot.watermark.strip())

    def test_replacement_changes_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_file(directory, "s.jsonl", b'{"a": 1}\n')
            before = read_jsonl_snapshot(path)
            replacement = self.write_file(directory, "replacement.jsonl", b'{"a": 1}\n')
            # Keep both files alive so inode reuse/coarse clocks cannot fake no change.
            self.assertNotEqual(os.stat(path).st_ino, os.stat(replacement).st_ino)
            os.replace(replacement, path)
            after = read_jsonl_snapshot(path)
            self.assertNotEqual(before.watermark, after.watermark)

    def test_native_rows_shape_validated(self) -> None:
        with self.assertRaises(ValueError):
            NativePerformanceRows((), -1, "w")
        with self.assertRaises(ValueError):
            NativePerformanceRows((), 0, "  ")
        rows = NativePerformanceRows(((1, {"a": 1}),), 1, "w")
        self.assertEqual(rows.entries_read, 1)


if __name__ == "__main__":
    unittest.main()
