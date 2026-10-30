"""U1 native usage observations: preserve, never collapse or total."""

from __future__ import annotations

import unittest

from sesskit.usage import (
    collect_claude_observations,
    collect_codex_observations,
    collect_opencode_observations,
    collect_pi_observations,
    describe_support,
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


if __name__ == "__main__":
    unittest.main()
