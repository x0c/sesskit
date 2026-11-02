"""U1 native usage observations: preserve, never collapse or total."""

from __future__ import annotations

import inspect
import unittest

from sesskit.usage import (
    collect_claude_observations,
    collect_codex_observations,
    collect_kimi_observations,
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
        for runtime in ("claude", "codex", "opencode", "pi", "kimi"):
            self.assertEqual(describe_support(runtime), "supported")

    def test_unsupported_stays_unsupported(self) -> None:
        self.assertEqual(describe_support("cursor"), "unsupported")
        self.assertEqual(describe_support("bogus-runtime"), "unsupported")


class SessionIdentityTests(unittest.TestCase):
    def test_qualified_ids_derive_from_session_and_record(self) -> None:
        rows = [(4, {"message": {"id": "m1", "model": "m",
                                 "usage": {"input_tokens": 10}}})]
        first = collect_claude_observations(rows, session_id="s1")
        second = collect_claude_observations(rows, session_id="s1")
        self.assertEqual(first[0].observation_id, "claude:s1:line:4:usage")
        self.assertEqual(first[0].observation_id, second[0].observation_id)
        other = collect_claude_observations(rows, session_id="s2")
        self.assertEqual(other[0].observation_id, "claude:s2:line:4:usage")

    def test_legacy_ids_without_session_keep_index_form(self) -> None:
        rows = [(4, {"message": {"id": "m1", "model": "m",
                                 "usage": {"input_tokens": 10}}})]
        observations = collect_claude_observations(rows)
        self.assertEqual(observations[0].observation_id, "claude:line:4:0")

    def test_duplicate_records_get_deterministic_suffixes(self) -> None:
        messages = [{"modelID": "m", "tokens": {"input": 1}},
                    {"modelID": "m", "tokens": {"input": 1}}]
        first = collect_opencode_observations(messages, session_id="s")
        second = collect_opencode_observations(messages, session_id="s")
        ids = [o.observation_id for o in first]
        self.assertEqual(ids[0], "opencode:s:message:unknown:tokens")
        self.assertEqual(ids[1], "opencode:s:message:unknown:tokens#2")
        self.assertEqual(ids, [o.observation_id for o in second])

    def test_codex_counters_share_record_with_distinct_fields(self) -> None:
        rows = [(7, {"type": "token_usage_record", "payload": {
            "thread_id": "th", "turn_id": "t", "response_id": "r",
            "usage": {"input_tokens": 1},
            "turn_token_usage": {"input_tokens": 1},
            "thread_token_usage": {"input_tokens": 1}}})]
        observations = collect_codex_observations(rows, session_id="s")
        ids = sorted(o.observation_id for o in observations)
        self.assertEqual(ids, ["codex:s:line:7:thread_token_usage",
                               "codex:s:line:7:turn_token_usage",
                               "codex:s:line:7:usage"])

    def test_kimi_and_pi_qualified_ids(self) -> None:
        kimi_rows = [(3, {"type": "usage.record", "model": "m",
                          "usage": {"inputOther": 1}})]
        kimi = collect_kimi_observations(kimi_rows, session_id="k")
        self.assertEqual(kimi[0].observation_id, "kimi:k:line:3:usage")
        pi_entries = [{"type": "message", "id": "a1", "message": {
            "role": "assistant", "model": "m", "content": "hi",
            "usage": {"input": 1}}}]
        pi = collect_pi_observations(pi_entries, session_id="p")
        self.assertEqual(pi[0].observation_id, "pi:p:entry:a1:usage")

    def test_cost_state_models_share_line_with_distinct_fields(self) -> None:
        rows = [(9, {"type": "cost-state", "sessionId": "s",
                     "modelUsage": {"m1": {"inputTokens": 1},
                                    "m2": {"inputTokens": 2}}})]
        observations = collect_claude_observations(rows, session_id="s")
        ids = sorted(o.observation_id for o in observations)
        self.assertEqual(ids, ["claude:s:line:9:modelUsage:m1",
                               "claude:s:line:9:modelUsage:m2"])


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

    def test_thinking_tokens_and_cache_tiers_preserved(self) -> None:
        rows = [
            (1, {"message": {"id": "m1", "model": "m", "usage": {
                "input_tokens": 2, "output_tokens": 170,
                "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
                "output_tokens_details": {"thinking_tokens": 65},
                "cache_creation": {"ephemeral_1h_input_tokens": 50,
                                   "ephemeral_5m_input_tokens": 0},
                "server_tool_use": {"web_search_requests": 3,
                                    "web_fetch_requests": 0}}}}),
        ]
        observations = collect_claude_observations(rows)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].usage.reasoning_tokens, 65)
        self.assertIn(("cache_creation_1h", 50), observations[0].native_subdivisions)
        self.assertIn(("cache_creation_5m", 0), observations[0].native_subdivisions)
        self.assertIn(("server_tool_web_search_requests", 3),
                      observations[0].native_subdivisions)
        normalized = normalize_observation(observations[0])
        self.assertEqual(normalized.reasoning, 65)
        self.assertEqual(normalized.output_total, 170)

    def test_cost_state_yields_one_session_observation_per_model(self) -> None:
        rows = [
            (9, {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.5,
                 "modelUsage": {
                     "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 100,
                                         "thinkingTokens": 40,
                                         "cacheReadInputTokens": 10,
                                         "cacheCreationInputTokens": 20,
                                         "costUSD": 1.4},
                     "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 10,
                                          "thinkingTokens": 0,
                                          "cacheReadInputTokens": 0,
                                          "cacheCreationInputTokens": 0,
                                          "costUSD": 0.1}}}),
        ]
        observations = collect_claude_observations(rows)
        self.assertEqual(len(observations), 2)
        for observation in observations:
            self.assertEqual(observation.scope_kind, "session")
            self.assertEqual(observation.counter_kind, "cumulative")
            self.assertEqual(observation.counter_name, "cost_state")
            self.assertEqual(observation.scope_id, "s1")
        by_model = {o.usage.model: o for o in observations}
        self.assertEqual(by_model["claude-opus-5-5"].usage.reasoning_tokens, 40)
        self.assertEqual(by_model["claude-opus-5-5"].usage.cost, 1.4)
        self.assertNotEqual(observations[0].observation_id,
                            observations[1].observation_id)

    def test_empty_model_usage_yields_no_observation(self) -> None:
        rows = [(3, {"type": "cost-state", "sessionId": "s1",
                     "totalCostUSD": 0, "modelUsage": {}})]
        self.assertEqual(collect_claude_observations(rows), [])

    def test_cost_state_revisions_fold_per_model_then_combine(self) -> None:
        def cost_row(lineno: int, opus_out: int, opus_cost: float) -> tuple:
            return (lineno, {"type": "cost-state", "sessionId": "s1",
                             "modelUsage": {
                                 "claude-opus-5-5": {
                                     "inputTokens": 2, "outputTokens": opus_out,
                                     "thinkingTokens": 10,
                                     "cacheReadInputTokens": 10,
                                     "cacheCreationInputTokens": 20,
                                     "costUSD": opus_cost},
                                 "claude-haiku-5-5": {
                                     "inputTokens": 5, "outputTokens": 10,
                                     "thinkingTokens": 0,
                                     "cacheReadInputTokens": 0,
                                     "cacheCreationInputTokens": 0,
                                     "costUSD": 0.1}}})
        rows = [cost_row(9, 100, 1.4), cost_row(12, 150, 2.0)]
        records = reconcile(collect_claude_observations(rows))
        session_records = [r for r in records if r.scope_kind == "session"]
        # One firm record per model (revisions folded), no conflicts.
        self.assertEqual(len(session_records), 2)
        self.assertTrue(all(r.standing == "firm" for r in session_records))
        by_model = {r.model: r for r in session_records}
        # Opus revised between rows; haiku rows were identical and collapsed.
        self.assertEqual(by_model["claude-opus-5-5"].revision, 1)
        self.assertEqual(by_model["claude-haiku-5-5"].revision, 0)
        summary = summarize(records, runtime="claude", observed_count=4)
        # opus latest: input 2+10+20=32, output 150; haiku: 5, output 10.
        self.assertEqual(summary.input_total.complete_total, 37)
        self.assertEqual(summary.output_total.complete_total, 160)
        self.assertEqual(summary.total_tokens.complete_total, 197)
        self.assertAlmostEqual(summary.cost.complete_total or 0.0, 2.1)
        self.assertIn("model_rollups_combined", summary.diagnostics)
        self.assertIsNotNone(summary.total_tokens.complete_total)

    def test_same_model_differing_rollups_conflict(self) -> None:
        rows = [
            (9, {"type": "cost-state", "sessionId": "s1",
                 "modelUsage": {"m": {"inputTokens": 2, "outputTokens": 100,
                                      "costUSD": 1.0}}}),
            (12, {"type": "cost-state", "sessionId": "other",
                  "modelUsage": {"m": {"inputTokens": 2, "outputTokens": 200,
                                       "costUSD": 2.0}}}),
        ]
        records = reconcile(collect_claude_observations(rows))
        summary = summarize(records, runtime="claude", observed_count=2)
        self.assertEqual(summary.coverage, "conflicted")
        self.assertIsNone(summary.total_tokens.complete_total)


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

    def test_thread_token_usage_maps_to_session_scope(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t1", "response_id": "r1",
                "usage": {"input_tokens": 100, "total_tokens": 110},
                "turn_token_usage": {"input_tokens": 100, "total_tokens": 110},
                "thread_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
        ]
        observations = collect_codex_observations(rows)
        sessions = [o for o in observations if o.scope_kind == "session"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].counter_name, "thread_token_usage")
        self.assertEqual(sessions[0].scope_id, "th1")
        self.assertIsNone(sessions[0].response_id)

    def test_thread_revisions_fold_to_latest_session_record(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t1", "response_id": "r1",
                "usage": {"input_tokens": 100, "total_tokens": 110},
                "turn_token_usage": {"input_tokens": 100, "total_tokens": 110},
                "thread_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (2, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t1", "response_id": "r2",
                "usage": {"input_tokens": 50, "total_tokens": 60},
                "turn_token_usage": {"input_tokens": 150, "total_tokens": 170},
                "thread_token_usage": {"input_tokens": 150, "total_tokens": 170}}}),
        ]
        observations = collect_codex_observations(rows)
        self.assertEqual(len(observations), 6)
        records = reconcile(observations)
        session_records = [r for r in records if r.scope_kind == "session"]
        self.assertEqual(len(session_records), 1)
        self.assertEqual(session_records[0].revision, 1)
        self.assertIn("cumulative_revision_latest_wins",
                      session_records[0].diagnostics)
        summary = summarize(records, runtime="codex", observed_count=6)
        self.assertEqual(summary.total_tokens.complete_total, 170)
        self.assertEqual(summary.total_tokens.known_total, 170)

    def test_thread_without_thread_id_stays_ambiguous(self) -> None:
        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "turn_id": "t1", "response_id": "r1",
                "usage": {"input_tokens": 100, "total_tokens": 110},
                "thread_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
        ]
        records = reconcile(collect_codex_observations(rows))
        session_records = [r for r in records if r.scope_kind == "session"]
        self.assertEqual(len(session_records), 1)
        self.assertEqual(session_records[0].standing, "ambiguous")
        summary = summarize(records, runtime="codex", observed_count=2)
        self.assertNotIn(session_records[0].usage_id, summary.selected_ids)


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

    def test_step_keeps_identity_without_joining_turn_sums(self) -> None:
        messages = [{"id": "m1", "modelID": "m",
                     "tokens": {"input": 10, "output": 4,
                                "cache": {"read": 0, "write": 0}}}]
        steps = [{"id": "s1", "modelID": "m",
                  "tokens": {"input": 10, "output": 4,
                             "cache": {"read": 0, "write": 0}}}]
        observations = collect_opencode_observations(messages, steps=steps)
        step = next(o for o in observations if o.operation_detail == "step")
        self.assertEqual(step.scope_kind, "unknown")
        self.assertEqual(step.scope_id, "s1")
        summary = summarize(reconcile(observations), runtime="opencode",
                            observed_count=2)
        # Message detail alone; the step stays inspectable but unsummed.
        self.assertEqual(summary.total_tokens.known_total, 14)
        self.assertTrue(summary.ambiguous_ids or "unscoped_excluded:1"
                        in summary.diagnostics)


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


class KimiTests(unittest.TestCase):
    def test_usage_record_collected_as_turn_delta(self) -> None:
        rows = [
            (29, {"type": "usage.record", "agentId": "main", "model": "localmodel",
                  "usage": {"inputOther": 15, "output": 2,
                            "inputCacheRead": 41789, "inputCacheCreation": 0},
                  "usageScope": "turn", "time": 1790772283338}),
        ]
        observations = collect_kimi_observations(rows)
        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation.scope_kind, "turn")
        self.assertEqual(observation.counter_kind, "absolute")
        self.assertEqual(observation.operation_detail, "usage.record")
        self.assertEqual(observation.usage.model, "localmodel")
        normalized = normalize_observation(observation)
        self.assertEqual(normalized.input_total, 41804)
        self.assertEqual(normalized.output_total, 2)
        self.assertEqual(normalized.total, 41806)

    def test_token_counting_measurements_not_collected(self) -> None:
        rows = [
            (30, {"type": "token_counting.measured", "agentId": "main",
                  "length": 4, "tokens": 41806, "time": 1790772283489}),
            (31, {"type": "token_counting.turn_recorded", "agentId": "main",
                  "turnId": 0, "length": 4, "tokens": 41806,
                  "time": 1790772284052}),
            (32, {"type": "llm.request", "agentId": "main", "kind": "loop",
                  "provider": "openai", "model": "qwen3.5-9b"}),
        ]
        self.assertEqual(collect_kimi_observations(rows), [])

    def test_turn_deltas_sum_across_source_positions(self) -> None:
        rows = [
            (10, {"type": "usage.record", "model": "m",
                  "usage": {"inputOther": 100, "output": 10,
                            "inputCacheRead": 0, "inputCacheCreation": 0},
                  "usageScope": "turn"}),
            (20, {"type": "usage.record", "model": "m",
                  "usage": {"inputOther": 50, "output": 5,
                            "inputCacheRead": 20, "inputCacheCreation": 0},
                  "usageScope": "turn"}),
        ]
        summary = summarize_observations(
            collect_kimi_observations(rows), runtime="kimi")
        self.assertEqual(summary.input_total.known_total, 170)
        self.assertEqual(summary.output_total.known_total, 15)
        self.assertEqual(summary.total_tokens.known_total, 185)
        self.assertIsNone(summary.total_tokens.complete_total)

    def test_empty_wire_reports_not_recorded(self) -> None:
        rows = [(1, {"type": "metadata"}), (2, {"type": "llm.request"})]
        summary = summarize_observations(
            collect_kimi_observations(rows), runtime="kimi")
        self.assertEqual(summary.coverage, "not_recorded")


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

    def test_turn_total_beside_finer_details_does_not_double_count(self) -> None:
        from sesskit.models import Evidence, Usage
        from sesskit.usage import UsageObservation
        call = UsageObservation(
            "kimi:r:0", "kimi", "r0", "call", "absolute", "model_call",
            Usage(Evidence("native"), cost=None, input_tokens=100,
                  output_tokens=10),
            Evidence("native"), scope_id="s", turn_id="t1")
        turn_total = UsageObservation(
            "kimi:r:1", "kimi", "r1", "turn", "absolute", "model_call",
            Usage(Evidence("native"), cost=None, input_tokens=100,
                  output_tokens=10),
            Evidence("native"), scope_id="s", turn_id="t1",
            operation_detail="usage.record")
        summary = summarize(reconcile([call, turn_total]), runtime="kimi",
                            observed_count=2)
        self.assertEqual(summary.total_tokens.known_total, 110)
        self.assertIn("turn_total_overlaps_details:t1", summary.diagnostics)

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
