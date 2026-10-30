"""U1 native usage observations: preserve, never collapse or total."""

from __future__ import annotations

import inspect
import unittest

from sesskit.usage import (
    collect_claude_observations,
    collect_codex_observations,
    collect_opencode_observations,
    collect_pi_observations,
    describe_support,
    normalize_observation,
    reconcile,
    summarize,
    summarize_observations,
)


class SupportTests(unittest.TestCase):
    def test_supported_runtimes(self) -> None:
        for runtime in ("claude", "codex", "opencode", "pi"):
            self.assertEqual(describe_support(runtime), "supported")

    def test_unsupported_stays_unsupported(self) -> None:
        self.assertEqual(describe_support("cursor"), "unsupported")
        self.assertEqual(describe_support("kimi"), "unsupported")


class ClaudeTests(unittest.TestCase):
    def test_repeated_message_id_keeps_both_observations(self) -> None:
        rows = [
            (1, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
            (2, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ]
        observations = collect_claude_observations(rows)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].response_id, "m1")
        self.assertEqual(observations[1].response_id, "m1")
        self.assertNotEqual(observations[0].observation_id, observations[1].observation_id)


class CodexTests(unittest.TestCase):
    def test_request_and_cumulative_revisions_all_preserved(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1", "model": "m",
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                "turn_token_usage": {"input_tokens": 100, "output_tokens": 10,
                                     "total_tokens": 110},
            }}),
            (2, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1", "model": "m",
                "turn_token_usage": {"input_tokens": 200, "output_tokens": 40,
                                     "total_tokens": 240},
            }}),
        ]
        observations = collect_codex_observations(rows)
        # request + first cumulative + revised cumulative: none collapsed.
        self.assertEqual(len(observations), 3)
        cumulatives = [o for o in observations if o.counter_kind == "cumulative"]
        self.assertEqual(len(cumulatives), 2)
        totals = sorted(o.usage.total_tokens or 0 for o in cumulatives)
        self.assertEqual(totals, [110, 240])

    def test_legacy_token_count_preserved_with_unknown_linkage(self) -> None:
        rows = [
            (7, {"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 5, "total_tokens": 8}},
            }}),
        ]
        observations = collect_codex_observations(rows)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].scope_kind, "call")


class OpenCodeTests(unittest.TestCase):
    def test_message_and_rollup_are_separate_scopes(self) -> None:
        messages = [{"id": "m1", "modelID": "m",
                     "tokens": {"input": 10, "output": 4,
                                "cache": {"read": 2, "write": 1},
                                "reasoning": 1}}]
        rollup = {"tokens": {"input": 10, "output": 4,
                             "cache": {"read": 2, "write": 1},
                             "reasoning": 1}}
        observations = collect_opencode_observations(messages, session_rollup=rollup)
        self.assertEqual(len(observations), 2)
        scopes = sorted(o.scope_kind for o in observations)
        self.assertEqual(scopes, ["message", "session"])


class PiTests(unittest.TestCase):
    def test_tool_only_assistant_still_observed(self) -> None:
        entries = [
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "model": "m", "provider": "p",
                "content": [{"type": "toolCall", "id": "c1", "name": "t"}],
                "usage": {"input": 50, "output": 6, "reasoning": 2,
                          "totalTokens": 56},
            }},
        ]
        observations = collect_pi_observations(entries)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].usage.reasoning_tokens, 2)
        self.assertEqual(observations[0].operation, "model_call")

    def test_tool_result_nested_usage_is_not_main_call(self) -> None:
        entries = [
            {"type": "message", "id": "t1", "message": {
                "role": "toolResult", "toolCallId": "c1",
                "content": "ok",
                "usage": {"input": 3, "output": 1, "totalTokens": 4},
            }},
        ]
        observations = collect_pi_observations(entries)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].operation, "tool_nested")

    def test_unknown_carrier_preserved_not_dropped(self) -> None:
        entries = [
            {"type": "usage", "id": "u1",
             "usage": {"input": 7, "totalTokens": 9}},
        ]
        observations = collect_pi_observations(entries)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].operation, "unknown")

    def test_unmapped_native_subdivision_preserved_not_summed(self) -> None:
        entries = [
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "model": "m",
                "content": "hi",
                "usage": {"input": 10, "output": 2, "totalTokens": 12,
                          "cacheWrite1h": 4},
            }},
        ]
        observations = collect_pi_observations(entries)
        self.assertEqual(observations[0].native_subdivisions, (("cacheWrite1h", 4),))
        normalized = normalize_observation(observations[0])
        self.assertEqual(normalized.input_total, 10)
        self.assertEqual(normalized.cache_write, None)


def _pi_observation(**usage: object) -> object:
    entries = [{"type": "message", "id": "a1", "message": {
        "role": "assistant", "model": "m", "content": "hi", "usage": dict(usage),
    }}]
    return collect_pi_observations(entries)[0]


class NormalizeTests(unittest.TestCase):
    def test_anthropic_inclusive_input_sums_present_parts(self) -> None:
        rows = [(1, {"message": {"id": "m1", "model": "m", "usage": {
            "input_tokens": 100, "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 200, "output_tokens": 50}}})]
        normalized = normalize_observation(collect_claude_observations(rows)[0])
        self.assertEqual(normalized.input_total, 1200)
        self.assertEqual(normalized.gaps, ())

    def test_missing_addends_are_gaps_not_zero(self) -> None:
        rows = [(1, {"message": {"id": "m1", "model": "m", "usage": {
            "input_tokens": 100, "output_tokens": 50}}})]
        normalized = normalize_observation(collect_claude_observations(rows)[0])
        self.assertEqual(normalized.input_total, 100)
        self.assertIn("input.cache_read", normalized.gaps)

    def test_reasoning_is_subset_never_added(self) -> None:
        normalized = normalize_observation(_pi_observation(
            input=70, output=80, reasoning=60, totalTokens=150))  # type: ignore[arg-type]
        self.assertEqual(normalized.output_total, 80)
        self.assertEqual(normalized.reasoning, 60)

    def test_opencode_visible_plus_reasoning_is_inclusive_output(self) -> None:
        messages = [{"id": "m1", "modelID": "m",
                     "tokens": {"input": 10, "output": 20, "reasoning": 60,
                                "cache": {"read": 0, "write": 0}}}]
        normalized = normalize_observation(collect_opencode_observations(messages)[0])
        self.assertEqual(normalized.output_total, 80)

    def test_codex_buckets_preserved_never_added(self) -> None:
        rows = [(1, {"type": "token_usage_record", "payload": {
            "response_id": "r1", "turn_id": "t1", "model": "m",
            "usage": {"input_tokens": 100, "cached_input_tokens": 30,
                      "output_tokens": 20, "reasoning_output_tokens": 5,
                      "total_tokens": 120}}})]
        normalized = normalize_observation(collect_codex_observations(rows)[0])
        self.assertEqual(normalized.input_total, 100)
        self.assertEqual(normalized.cache_read, 30)
        self.assertEqual(normalized.output_total, 20)
        self.assertIsNone(normalized.total)
        self.assertEqual(normalized.reported_total, 120)

    def test_explicit_zero_cost_stays_zero_missing_stays_missing(self) -> None:
        with_cost = collect_opencode_observations(
            [{"id": "m1", "tokens": {"input": 1}, "cost": 0.0}])[0]
        without_cost = collect_opencode_observations(
            [{"id": "m1", "tokens": {"input": 1}}])[0]
        self.assertEqual(normalize_observation(with_cost).cost, 0.0)
        self.assertIsNone(normalize_observation(without_cost).cost)


class ReconcileTests(unittest.TestCase):
    def test_same_response_repeated_rows_become_one_record(self) -> None:
        rows = [
            (1, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
            (2, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ]
        records = reconcile(collect_claude_observations(rows))
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0].observation_ids), 2)
        self.assertEqual(records[0].standing, "firm")

    def test_cumulative_revisions_fold_to_latest_not_sum(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 100, "total_tokens": 100}}}),
            (2, {"type": "token_usage_record", "payload": {
                "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 240, "total_tokens": 240}}}),
            (3, {"type": "token_usage_record", "payload": {
                "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 310, "total_tokens": 310}}}),
        ]
        records = reconcile(collect_codex_observations(rows))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].revision, 2)
        summary = summarize(records, runtime="codex", observed_count=3)
        self.assertEqual(summary.total_tokens.known_total, 310)

    def test_differing_values_same_identity_conflict(self) -> None:
        rows = [
            (1, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
            (2, {"message": {"id": "m1", "model": "m",
                            "usage": {"input_tokens": 11, "output_tokens": 5}}}),
        ]
        records = reconcile(collect_claude_observations(rows))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.standing == "conflicted" for record in records))
        summary = summarize(records, runtime="claude", observed_count=2)
        self.assertEqual(summary.coverage, "conflicted")
        self.assertIsNone(summary.input_total.known_total)

    def test_legacy_record_without_linkage_stays_ambiguous(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (7, {"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "total_tokens": 110}}}}),
        ]
        records = reconcile(collect_codex_observations(rows))
        self.assertEqual(len(records), 2)
        summary = summarize(records, runtime="codex", observed_count=2)
        self.assertEqual(len(summary.ambiguous_ids), 1)
        self.assertEqual(summary.total_tokens.known_total, 110)


class SummaryTests(unittest.TestCase):
    def _turn_records(self) -> list:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (2, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
        ]
        return reconcile(collect_codex_observations(rows))

    def test_request_plus_rollup_counts_once(self) -> None:
        summary = summarize(self._turn_records(), runtime="codex", observed_count=2)
        self.assertEqual(summary.total_tokens.known_total, 110)
        self.assertEqual(summary.total_tokens.complete_total, None)

    def test_session_rollup_sets_scope_total_with_unattributed(self) -> None:
        messages = [{"id": "m1", "modelID": "m",
                     "tokens": {"input": 200, "output": 150,
                                "cache": {"read": 0, "write": 0}}}]
        rollup = {"tokens": {"input": 300, "output": 200,
                             "cache": {"read": 0, "write": 0}}}
        summary = summarize_observations(
            collect_opencode_observations(messages, session_rollup=rollup),
            runtime="opencode")
        self.assertEqual(summary.total_tokens.complete_total, 500)
        self.assertEqual(summary.total_tokens.detail_total, 350)
        self.assertEqual(summary.total_tokens.unattributed, 150)
        self.assertEqual(summary.coverage, "partial")

    def test_details_exceeding_rollup_conflict(self) -> None:
        messages = [{"id": "m1", "modelID": "m",
                     "tokens": {"input": 400, "output": 200,
                                "cache": {"read": 0, "write": 0}}}]
        rollup = {"tokens": {"input": 300, "output": 200,
                             "cache": {"read": 0, "write": 0}}}
        summary = summarize_observations(
            collect_opencode_observations(messages, session_rollup=rollup),
            runtime="opencode")
        self.assertEqual(summary.total_tokens.coverage, "conflicted")
        self.assertIsNone(summary.total_tokens.complete_total)

    def test_turn_interval_without_details_contributes(self) -> None:
        rows = [(1, {"type": "token_usage_record", "payload": {
            "turn_id": "t9",
            "turn_token_usage": {"input_tokens": 40, "total_tokens": 45}}})]
        summary = summarize_observations(collect_codex_observations(rows), runtime="codex")
        self.assertEqual(summary.total_tokens.known_total, 45)
        self.assertEqual(len(summary.interval_ids), 1)

    def test_turn_cumulative_is_authoritative_over_details(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "usage": {"input_tokens": 100, "total_tokens": 100}}}),
            (2, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 240, "total_tokens": 240}}}),
        ]
        summary = summarize_observations(collect_codex_observations(rows), runtime="codex")
        self.assertEqual(summary.total_tokens.detail_total, 100)
        self.assertEqual(summary.total_tokens.known_total, 240)

    def test_turn_details_exceeding_interval_conflict(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "usage": {"input_tokens": 300, "total_tokens": 300}}}),
            (2, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "turn_token_usage": {"input_tokens": 200, "total_tokens": 200}}}),
        ]
        summary = summarize_observations(collect_codex_observations(rows), runtime="codex")
        self.assertEqual(summary.total_tokens.coverage, "conflicted")
        self.assertIsNone(summary.total_tokens.known_total)

    def test_empty_history_reports_not_recorded(self) -> None:
        summary = summarize([], runtime="pi", observed_count=0)
        self.assertEqual(summary.coverage, "not_recorded")
        self.assertIsNone(summary.total_tokens.known_total)

    def test_unsupported_runtime_reports_unsupported(self) -> None:
        summary = summarize([], runtime="cursor", observed_count=0)
        self.assertEqual(summary.coverage, "unsupported")

    def test_cost_groups_by_basis_do_not_combine(self) -> None:
        from sesskit.models import Evidence, Usage
        from sesskit.usage import UsageObservation
        first = UsageObservation(
            "codex:r:0", "codex", "r0", "call", "absolute", "model_call",
            Usage(Evidence("native"), cost=1.0), Evidence("native"),
            response_id="req-1",
            cost_basis="provider_reported")
        second = UsageObservation(
            "codex:r:1", "codex", "r1", "call", "absolute", "model_call",
            Usage(Evidence("native"), cost=2.0), Evidence("native"),
            response_id="req-2",
            cost_basis="runtime_estimate")
        summary = summarize(reconcile([first, second]), runtime="codex",
                            observed_count=2)
        self.assertIsNone(summary.cost.known_total)
        self.assertEqual(len(summary.cost.groups), 2)

    def test_model_breakdown_sums_details(self) -> None:
        entries = [
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "model": "m1",
                "content": "hi", "usage": {"input": 10, "output": 5,
                                           "totalTokens": 15}}},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "model": "m2",
                "content": "hi", "usage": {"input": 20, "output": 5,
                                           "totalTokens": 25}}},
        ]
        summary = summarize_observations(collect_pi_observations(entries), runtime="pi")
        by_model = {slice.model: slice for slice in summary.by_model}
        self.assertEqual(by_model["m1"].input_total, 10)
        self.assertEqual(by_model["m2"].input_total, 20)
        self.assertEqual(summary.input_total.known_total, 30)


class GeneralityTests(unittest.TestCase):
    def test_core_has_no_runtime_name_branches(self) -> None:
        from sesskit import usage as usage_mod
        for name in ("reconcile", "summarize"):
            source = inspect.getsource(getattr(usage_mod, name))
            for literal in ("'claude'", '"claude"', "'codex'", '"codex"',
                            "'opencode'", '"opencode"', "'pi'", '"pi"',
                            "'cursor'", '"cursor"', "'kimi'", '"kimi"'):
                self.assertNotIn(literal, source, f"{name} mentions {literal}")
            self.assertNotIn("runtime ==", source)


if __name__ == "__main__":
    unittest.main()
