"""Model attribution pinning: provider merging, legacy scope, B1 thread, v1 parity, requested_model.

Covers R2+M5/M6 and R3+M2 gaps without touching existing assertions.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


class PiProviderMergeTests(unittest.TestCase):
    def test_carrier_usage_level_provider_flows_to_record(self) -> None:
        from sesskit.usage import (
            _observation_labels,
            collect_pi_observations,
            reconcile,
            summarize,
        )

        for entry_type in ("compaction", "branch_summary", "usage"):
            with self.subTest(carrier=entry_type):
                entries = [{
                    "type": entry_type,
                    "id": f"c-{entry_type}",
                    "provider": "pc",
                    "thinkingLevel": "high",
                    "usage": {"input": 7, "output": 3, "totalTokens": 10},
                }]
                observations = collect_pi_observations(entries, session_id="p")
                self.assertEqual(len(observations), 1)
                obs = observations[0]
                # Carrier attribution lands on both layers.
                self.assertEqual(obs.provider, "pc")
                self.assertEqual(obs.reasoning_level, "high")
                self.assertEqual(obs.usage.provider, "pc")
                self.assertEqual(obs.usage.reasoning_level, "high")
                labels = _observation_labels(obs)
                self.assertEqual(labels["provider"], "pc")
                self.assertEqual(labels["reasoning_level"], "high")
                records = reconcile(observations)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].provider, "pc")
                self.assertEqual(records[0].reasoning_level, "high")
                # Carriers stay inspectable but unsummed.
                summary = summarize(records, runtime="pi", observed_count=len(observations))
                self.assertIn("unscoped_excluded:1", summary.diagnostics)
                self.assertIsNone(summary.input_total.known_total)
                self.assertNotIn(records[0].usage_id, summary.selected_ids)

    def test_observation_labels_obs_wins_over_usage(self) -> None:
        from sesskit.models import Evidence, Usage
        from sesskit.usage import (
            UsageObservation,
            _observation_labels,
            observation_to_dict,
            reconcile,
        )

        obs = UsageObservation(
            "pi:r:0", "pi", "r0", "message", "absolute", "model_call",
            Usage(Evidence("native"), model="u-model", requested_model="u-req",
                  provider="u-prov", reasoning_level="u-lvl"),
            Evidence("native"),
            requested_model="obs-req",
            provider="",  # empty counts as absent -> falls back to usage
            reasoning_level="obs-lvl",
        )
        labels = _observation_labels(obs)
        self.assertEqual(labels["requested_model"], "obs-req")
        self.assertEqual(labels["provider"], "u-prov")
        self.assertEqual(labels["reasoning_level"], "obs-lvl")
        payload = observation_to_dict(obs)
        self.assertEqual(payload["requested_model"], "obs-req")
        self.assertEqual(payload["provider"], "u-prov")
        self.assertEqual(payload["reasoning_level"], "obs-lvl")
        records = reconcile([obs])
        self.assertEqual(records[0].requested_model, "obs-req")
        self.assertEqual(records[0].provider, "u-prov")
        self.assertEqual(records[0].reasoning_level, "obs-lvl")

    def test_by_model_splits_on_provider(self) -> None:
        from sesskit.usage import collect_pi_observations, summarize_observations

        entries = [
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "model": "m", "provider": "p1",
                "content": "hi", "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "model": "m", "provider": "p2",
                "content": "hi", "usage": {"input": 20, "output": 5, "totalTokens": 25}}},
        ]
        summary = summarize_observations(
            collect_pi_observations(entries, session_id="p"), runtime="pi")
        by_model = {(s.model, s.provider): s for s in summary.by_model}
        self.assertIn(("m", "p1"), by_model)
        self.assertIn(("m", "p2"), by_model)
        self.assertEqual(by_model[("m", "p1")].input_total, 10)
        self.assertEqual(by_model[("m", "p2")].input_total, 20)
        self.assertEqual(summary.input_total.known_total, 30)


class CodexLegacyTotalTests(unittest.TestCase):
    def test_legacy_total_scope_unknown_and_excluded(self) -> None:
        from sesskit.usage import collect_codex_observations, reconcile, summarize

        for key in ("total_token_usage", "total_usage"):
            with self.subTest(key=key):
                rows = [(7, {"type": "event_msg", "payload": {
                    "type": "token_count", "info": {key: {"input_tokens": 5, "total_tokens": 8}}}})]
                observations = collect_codex_observations(rows)
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0].scope_kind, "unknown")
                self.assertEqual(observations[0].counter_kind, "cumulative")
                records = reconcile(observations)
                self.assertEqual(len(records), 1)
                summary = summarize(records, runtime="codex", observed_count=1)
                self.assertIsNone(summary.total_tokens.known_total)
                self.assertNotIn(records[0].usage_id, summary.selected_ids)
                # No-linkage legacy stays out of sums (ambiguous today,
                # unscoped for firm unknown-scope records).
                self.assertTrue(
                    any(d.startswith(("ambiguous_excluded", "unscoped_excluded"))
                        for d in summary.diagnostics),
                    summary.diagnostics,
                )

    def test_legacy_last_vs_total_kinds(self) -> None:
        from sesskit.usage import collect_codex_observations

        rows = [(7, {"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 5, "total_tokens": 8}}}})]
        obs = collect_codex_observations(rows)
        self.assertEqual(obs[0].scope_kind, "call")
        self.assertEqual(obs[0].counter_kind, "absolute")
        rows_total = [(7, {"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 5, "total_tokens": 8}}}})]
        obs_total = collect_codex_observations(rows_total)
        self.assertEqual(obs_total[0].scope_kind, "unknown")
        self.assertEqual(obs_total[0].counter_kind, "cumulative")

    def test_legacy_total_does_not_join_known(self) -> None:
        from sesskit.usage import collect_codex_observations, summarize_observations

        rows = [
            (1, {"type": "token_usage_record", "payload": {
                "response_id": "r1", "turn_id": "t1",
                "usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (7, {"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 5, "total_tokens": 8}}}}),
        ]
        summary = summarize_observations(
            collect_codex_observations(rows), runtime="codex")
        # Only the firm call detail contributes; the unlinked legacy total stays out.
        self.assertEqual(summary.total_tokens.known_total, 110)


class B1ThreadTests(unittest.TestCase):
    def test_thread_same_model_folds_to_single_record(self) -> None:
        from sesskit.usage import collect_codex_observations, reconcile, summarize

        rows = [
            (1, {"type": "turn_context", "payload": {"turn_id": "t1", "model": "model-a"}}),
            (2, {"type": "turn_context", "payload": {"turn_id": "t2", "model": "model-a"}}),
            (3, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t1",
                "thread_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (4, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t2",
                "thread_token_usage": {"input_tokens": 150, "total_tokens": 170}}}),
        ]
        observations = collect_codex_observations(rows, session_id="s")
        self.assertEqual(len(observations), 2)
        records = reconcile(observations)
        sessions = [r for r in records if r.scope_kind == "session"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].revision, 1)
        # Thread snapshots stay isolated from turn-context backfill: one
        # thread counter never splits into per-model partitions.
        self.assertIsNone(sessions[0].model)
        self.assertIsNone(sessions[0].requested_model)
        summary = summarize(records, runtime="codex", observed_count=len(observations))
        self.assertEqual(summary.total_tokens.complete_total, 170)
        # Single latest revision, not a doubled sum (110 + 170 = 280).
        self.assertNotEqual(summary.total_tokens.complete_total, 280)

    def test_thread_observations_carry_no_turn_linkage(self) -> None:
        from sesskit.usage import collect_codex_observations

        rows = [
            (1, {"type": "turn_context", "payload": {"turn_id": "t1", "model": "model-a"}}),
            (2, {"type": "turn_context", "payload": {"turn_id": "t2", "model": "model-b"}}),
            (3, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t1",
                "thread_token_usage": {"input_tokens": 100, "total_tokens": 110}}}),
            (4, {"type": "token_usage_record", "payload": {
                "thread_id": "th1", "turn_id": "t2",
                "thread_token_usage": {"input_tokens": 150, "total_tokens": 170}}}),
        ]
        observations = collect_codex_observations(rows, session_id="s")
        threads = [o for o in observations if o.scope_kind == "session"]
        self.assertEqual(len(threads), 2)
        for obs in threads:
            self.assertIsNone(obs.turn_id)
            self.assertIsNone(obs.response_id)
            self.assertEqual(obs.scope_id, "th1")
            self.assertEqual(obs.counter_name, "thread_token_usage")


class V1ParityTests(unittest.TestCase):
    def _pi_session(self, directory: Path) -> dict:
        path = directory / "pi.jsonl"
        _write_jsonl(path, [
            {"type": "session", "id": "pi-1", "timestamp": "2026-09-01T00:00:00Z",
             "cwd": "/tmp/pi-parity"},
            {"type": "message", "id": "u1", "timestamp": "2026-09-01T00:00:01Z",
             "message": {"role": "user", "content": "Run checks."}},
            {"type": "message", "id": "a1", "parentId": "u1",
             "timestamp": "2026-09-01T00:00:02Z",
             "message": {"role": "assistant", "content": "Running."}},
        ])
        return {"source": "pi", "path": str(path), "id": "pi-1"}

    def _claude_session(self, directory: Path) -> dict:
        path = directory / "claude.jsonl"
        _write_jsonl(path, [
            {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
             "origin": {"kind": "human"},
             "message": {"role": "user", "content": "Run checks."}},
            {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Running."}]}},
        ])
        return {"source": "claude", "path": str(path), "id": "claude-1"}

    def _opencode_session(self, directory: Path) -> dict:
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
        conn.execute(
            "INSERT INTO message VALUES ('m1', 's1', 1000, ?)",
            (json.dumps({"role": "user", "time": {"created": 1000}}),),
        )
        conn.execute(
            "INSERT INTO message VALUES ('m2', 's1', 2000, ?)",
            (json.dumps({"role": "assistant", "finish": "stop",
                         "time": {"created": 2000}}),),
        )
        parts = [
            ("p1", "m1", 1000, {"type": "text", "text": "Run checks."}),
            ("p2", "m2", 2000, {"type": "text", "text": "Running."}),
        ]
        for part_id, message_id, stamp, data in parts:
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (part_id, message_id, "s1", stamp, json.dumps(data)),
            )
        conn.commit()
        conn.close()
        return {"source": "opencode", "path": str(path), "id": "s1"}

    def test_to_v1_dicts_matches_load_events(self) -> None:
        from sesskit.activity import load_activity, to_v1_dicts
        from sesskit.transcript import load_events

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for builder in (self._pi_session, self._claude_session, self._opencode_session):
                with self.subTest(builder=builder.__name__):
                    session = builder(directory)
                    snapshot = load_activity(session)
                    self.assertEqual(snapshot.state, "available")
                    self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_messages_item_key_set(self) -> None:
        from sesskit.usage import (
            assemble_snapshot,
            collect_pi_observations,
            iter_message_models,
            observation_to_dict,
            record_to_dict,
        )

        entries = [
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "model": "m1", "provider": "p1",
                "content": "hi", "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
        ]
        observations = collect_pi_observations(entries, session_id="p")
        snapshot = assemble_snapshot(
            runtime="pi", session_ref="pi:p", history_view="all_branches",
            observations=observations, entries_read=1, watermark="w1",
        )
        items = iter_message_models(snapshot, None)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(
                set(item.keys()),
                {"seq", "message_id", "response_id", "turn_id",
                 "model", "requested_model", "provider", "reasoning_level", "attribution"},
            )
        obs_keys = set(observation_to_dict(observations[0]).keys())
        for key in ("provider", "requested_model", "api", "reasoning_level",
                    "service_tier", "model_variant"):
            self.assertIn(key, obs_keys)
        rec_keys = set(record_to_dict(snapshot.records[0]).keys())
        for key in ("provider", "requested_model", "api", "reasoning_level",
                    "service_tier", "model_variant"):
            self.assertIn(key, rec_keys)


class RequestedModelTests(unittest.TestCase):
    def test_claude_requested_model_separate_from_model(self) -> None:
        from sesskit.usage import (
            UsageSelection,
            assemble_snapshot,
            collect_claude_observations,
            reconcile,
            summarize_usage,
        )

        rows = [(1, {"requestedModel": "req-m",
                     "perTurnEffort": "high",
                     "message": {"id": "m1", "model": "eff-m",
                                 "usage": {"input_tokens": 10, "output_tokens": 5,
                                           "service_tier": "flex"}}})]
        observations = collect_claude_observations(rows)
        self.assertEqual(observations[0].usage.model, "eff-m")
        self.assertEqual(observations[0].usage.requested_model, "req-m")
        self.assertEqual(observations[0].requested_model, "req-m")
        self.assertEqual(observations[0].reasoning_level, "high")
        records = reconcile(observations)
        self.assertEqual(records[0].model, "eff-m")
        self.assertEqual(records[0].requested_model, "req-m")
        snapshot = assemble_snapshot(
            runtime="claude", session_ref="claude:s1", history_view="all_branches",
            observations=observations, entries_read=1, watermark="w1",
        )
        self.assertEqual(
            summarize_usage(snapshot, selection=UsageSelection(models=("eff-m",))).record_count, 1)
        self.assertEqual(
            summarize_usage(snapshot, selection=UsageSelection(models=("req-m",))).record_count, 1)
        self.assertEqual(
            summarize_usage(snapshot, selection=UsageSelection(models=("other",))).record_count, 0)

    def test_codex_backfill_present_and_native_wins(self) -> None:
        from sesskit import cli as cli_mod
        from sesskit.usage import collect_codex_observations, reconcile

        backfilled = collect_codex_observations([
            (1, {"type": "turn_context",
                 "payload": {"turn_id": "t1", "model": "ctx-model", "effort": "high"}}),
            (2, {"type": "token_usage_record", "payload": {
                "turn_id": "t1", "response_id": "r1",
                "usage": {"input_tokens": 10, "total_tokens": 12}}}),
        ])
        self.assertTrue(backfilled)
        effective = backfilled[0].usage.model or backfilled[0].usage.requested_model
        self.assertEqual(effective, "ctx-model")
        records = reconcile(backfilled)
        matched = [r for r in records
                   if r.model == "ctx-model" or r.requested_model == "ctx-model"]
        self.assertTrue(matched)
        self.assertTrue(cli_mod._filter_records_by_model(records, ["ctx-model"]))

        native = collect_codex_observations([
            (1, {"type": "turn_context",
                 "payload": {"turn_id": "t1", "model": "ctx-model", "effort": "high"}}),
            (2, {"type": "token_usage_record", "payload": {
                "turn_id": "t1", "response_id": "r1", "model": "native-model",
                "usage": {"input_tokens": 10, "total_tokens": 12}}}),
        ])
        self.assertEqual(native[0].usage.model, "native-model")

    def test_kimi_llm_request_total_none_and_filter(self) -> None:
        from sesskit import cli as cli_mod
        from sesskit.usage import (
            collect_kimi_observations,
            normalize_observation,
            reconcile,
        )

        rows = [(32, {"type": "llm.request", "agentId": "main", "kind": "loop",
                      "provider": "openai", "model": "qwen3.5-9b",
                      "thinkingEffort": "high"})]
        observations = collect_kimi_observations(rows)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].operation_detail, "llm_request")
        self.assertIsNone(normalize_observation(observations[0]).total)
        effective = observations[0].usage.model or observations[0].usage.requested_model
        self.assertEqual(effective, "qwen3.5-9b")
        records = reconcile(observations)
        self.assertTrue(cli_mod._filter_records_by_model(records, ["qwen3.5-9b"]))
        self.assertTrue(cli_mod._filter_observations_by_model(observations, ["qwen3.5-9b"]))
        self.assertFalse(cli_mod._filter_records_by_model(records, ["other"]))

    def test_model_filter_hits_both_model_and_requested(self) -> None:
        from sesskit import cli as cli_mod
        from sesskit.models import Evidence, Usage
        from sesskit.usage import UsageObservation, UsageSelection, assemble_snapshot, summarize_usage

        def _obs(oid: str, model: str | None, requested: str | None) -> UsageObservation:
            return UsageObservation(
                oid, "claude", "r", "message", "absolute", "model_call",
                Usage(Evidence("native"), model=model, requested_model=requested,
                      input_tokens=5, output_tokens=2),
                Evidence("native"), scope_id=oid, message_id=oid,
                requested_model=requested,
            )

        observations = [_obs("o1", "eff-m", None), _obs("o2", "other-m", "req-m")]
        snapshot = assemble_snapshot(
            runtime="claude", session_ref="claude:s1", history_view="all_branches",
            observations=observations, entries_read=2, watermark="w1",
        )
        self.assertEqual(
            summarize_usage(snapshot, selection=UsageSelection(models=("eff-m",))).record_count, 1)
        self.assertEqual(
            summarize_usage(snapshot, selection=UsageSelection(models=("req-m",))).record_count, 1)
        self.assertEqual(len(cli_mod._filter_records_by_model(
            list(snapshot.records), ["req-m"])), 1)
        self.assertEqual(len(cli_mod._filter_observations_by_model(
            observations, ["req-m"])), 1)
        self.assertEqual(len(cli_mod._filter_records_by_model(
            list(snapshot.records), ["eff-m"])), 1)
        # Hook rows degrade gracefully but still honor requested_model when present.
        self.assertTrue(cli_mod._hook_item_matches_model(
            {"model": "other-m", "requested_model": "req-m"}, {"req-m"}))
        self.assertFalse(cli_mod._hook_item_matches_model(
            {"model": "other-m", "requested_model": "req-m"}, {"missing"}))


if __name__ == "__main__":
    unittest.main()
