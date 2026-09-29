"""Stage C: turns, error taxonomy, typed event union, tool invocations."""

from __future__ import annotations

import unittest

from sesskit.errors import (
    classify_agent_error,
    classify_error,
    native_http_status,
)
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    AgentError,
    Evidence,
    InteractionRequest,
    SessionOutcome,
    ToolResultOutcome,
    TurnOutcome,
    as_typed,
)
from sesskit.turns import (
    derive_session_outcome,
    derive_turns,
    error_views,
    pair_invocations,
    with_turn_ids,
)
from sesskit.visibility import visible_in_conversation, visible_in_v1

NATIVE = Evidence("native", field="f", record="r:1")
UNKNOWN_EVIDENCE = Evidence("unknown")


def _event(seq, type, **fields):
    return ActivityEvent(seq=seq, type=type, evidence=NATIVE, **fields)


def _snapshot(events, outcome=None):
    return ActivitySnapshot(
        state="available",
        events=tuple(events),
        outcome=outcome or SessionOutcome("done", Evidence("inferred", field="tail")),
    )


def _err(kind, message, code=None):
    return AgentError(kind, message, NATIVE, code)


class ClassifyErrorTest(unittest.TestCase):
    def test_codex_quota(self):
        kind, retryable = classify_error(
            runtime="codex", kind="provider", code="usage_limit_exceeded",
            message="Usage limit exceeded",
        )
        self.assertEqual(kind, "quota_exhausted")
        self.assertFalse(retryable)

    def test_opencode_rate_limited(self):
        kind, retryable = classify_error(
            runtime="opencode", kind="APIError", code="429",
            message="429: too many requests, slow down",
        )
        self.assertEqual(kind, "rate_limited")
        self.assertTrue(retryable)

    def test_opencode_auth(self):
        kind, retryable = classify_error(
            runtime="opencode", kind="ProviderAuthError", code="401",
            message="401: API key is invalid.",
        )
        self.assertEqual(kind, "auth")
        self.assertFalse(retryable)

    def test_opencode_output_length(self):
        kind, _ = classify_error(
            runtime="opencode", kind="MessageOutputLengthError",
            message="Output length exceeded.",
        )
        self.assertEqual(kind, "context_length")

    def test_opencode_abort_is_interrupt(self):
        kind, retryable = classify_error(
            runtime="opencode", kind="MessageAbortedError",
            message="The operation was aborted.",
        )
        self.assertEqual(kind, "user_interrupt")
        self.assertFalse(retryable)

    def test_claude_interrupt_marker(self):
        kind, _ = classify_error(
            runtime="claude", kind="aborted",
            message="[Request interrupted by user]",
        )
        self.assertEqual(kind, "user_interrupt")

    def test_claude_session_limit_is_quota(self):
        kind, _ = classify_error(
            runtime="claude", kind="provider",
            message="You've hit your session limit",
        )
        self.assertEqual(kind, "quota_exhausted")

    def test_claude_upstream_401(self):
        kind, _ = classify_error(
            runtime="claude", kind="provider", code="401",
            message="401 API key is invalid.",
        )
        self.assertEqual(kind, "auth")

    def test_pi_weekly_429(self):
        kind, retryable = classify_error(
            runtime="pi", kind="error",
            message="weekly limit 429 too many requests",
        )
        # Quota wording wins over the bare 429 when both appear.
        self.assertEqual(kind, "quota_exhausted")
        self.assertFalse(retryable)

    def test_pi_pure_429_is_rate_limited(self):
        kind, retryable = classify_error(
            runtime="pi", kind="error", message="429 Too Many Requests",
        )
        self.assertEqual(kind, "rate_limited")
        self.assertTrue(retryable)

    def test_overloaded(self):
        kind, retryable = classify_error(message="The server is overloaded (529)")
        self.assertEqual(kind, "provider_overloaded")
        self.assertTrue(retryable)

    def test_legacy_provider_kind_reachable(self):
        kind, _ = classify_error(kind="provider", message="boom 500")
        self.assertEqual(kind, "provider_error")

    def test_unclassifiable_stays_unknown(self):
        kind, retryable = classify_error(
            kind="weird", message="something indescribable happened",
        )
        self.assertEqual(kind, "unknown")
        self.assertIsNone(retryable)

    def test_agent_error_convenience(self):
        kind, _ = classify_agent_error(_err("aborted", "[Request interrupted by user]"))
        self.assertEqual(kind, "user_interrupt")

    def test_policy_blocked(self):
        kind, retryable = classify_error(message="Blocked by content policy")
        self.assertEqual(kind, "policy_blocked")
        self.assertFalse(retryable)

    def test_timeout(self):
        kind, retryable = classify_error(message="Request timed out after 120s")
        self.assertEqual(kind, "timeout")
        self.assertTrue(retryable)

    def test_refusal_is_never_an_error(self):
        kind, _ = classify_error(message="Request denied by user")
        self.assertEqual(kind, "unknown")
        kind, _ = classify_error(message="The model refused to comply")
        self.assertEqual(kind, "unknown")

    def test_filesystem_denial_without_user_stays_tool_error(self):
        kind, _ = classify_error(message="permission denied: /root/x")
        self.assertEqual(kind, "tool_error")

    def test_native_http_status(self):
        self.assertEqual(native_http_status("429", "slow down"), 429)
        self.assertEqual(native_http_status(None, "500 internal error"), 500)
        self.assertIsNone(native_http_status(None, "no code here"))
        self.assertIsNone(native_http_status(True, None))

    def test_error_scope_and_http_status_validated(self):
        error = AgentError("x", "m", NATIVE, scope="turn", http_status=429)
        self.assertEqual(error.scope, "turn")
        self.assertEqual(error.http_status, 429)
        with self.assertRaises(ValueError):
            AgentError("x", "m", NATIVE, scope="session-group")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AgentError("x", "m", NATIVE, http_status=99)

    def test_interaction_polarity_fields(self):
        request = InteractionRequest(
            "permission", NATIVE, resolution="approved", decided_by="human",
        )
        self.assertEqual(request.resolution, "approved")
        self.assertEqual(request.decided_by, "human")
        with self.assertRaises(ValueError):
            InteractionRequest("permission", NATIVE, decided_by="model")  # type: ignore[arg-type]

    def test_elicitation_split(self):
        form = InteractionRequest("elicitation_form", NATIVE)
        url = InteractionRequest("elicitation_url", NATIVE)
        self.assertEqual(form.purpose, "elicitation_form")
        self.assertEqual(url.purpose, "elicitation_url")
        legacy = InteractionRequest("elicitation", NATIVE)
        self.assertEqual(legacy.purpose, "elicitation")


class VisibilityTest(unittest.TestCase):
    def test_injected_hidden_everywhere(self):
        event = _event(1, "user_message", text="chrome", origin="injected")
        self.assertFalse(visible_in_conversation(event))
        self.assertFalse(visible_in_v1(event))

    def test_error_only_hidden_by_default(self):
        error = AgentError("timeout", "slow", NATIVE)
        event = _event(2, "assistant_message", text="slow", error=error)
        self.assertFalse(visible_in_conversation(event))
        self.assertTrue(visible_in_conversation(event, include_errors=True))
        self.assertTrue(visible_in_v1(event))

    def test_lifecycle_and_compaction_typed_only(self):
        marker = _event(3, "lifecycle", text="task_complete")
        self.assertFalse(visible_in_conversation(marker))
        self.assertFalse(visible_in_v1(marker))
        compaction = _event(4, "compaction", text="summary")
        self.assertFalse(visible_in_v1(compaction))


class DeriveTurnsTest(unittest.TestCase):
    def test_two_turns(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="a1"),
            _event(3, "user_message", text="q2"),
            _event(4, "assistant_message", text="a2"),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(len(turns), 2)
        self.assertEqual(
            [(t.index, t.start_seq, t.end_seq, t.user_seq) for t in turns],
            [(0, 1, 2, 1), (1, 3, 4, 3)],
        )
        self.assertEqual(turns[0].turn_id, "turn-0000")
        self.assertEqual([t.outcome.status for t in turns], ["completed", "completed"])

    def test_pending_tail_is_in_progress(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="a1"),
            _event(3, "user_message", text="q2"),
        ])
        turns = derive_turns(snapshot)
        # A bare trailing user message is weak evidence: in_progress, never
        # awaiting_input; the session projection still reads pending.
        self.assertEqual(
            [t.outcome.status for t in turns], ["completed", "in_progress"],
        )
        derived = derive_session_outcome(turns, snapshot.outcome)
        self.assertEqual(derived.status, "pending")

    def test_pending_alias_still_accepted(self):
        outcome = TurnOutcome("pending", Evidence("inferred", field="tail_role"))  # type: ignore[arg-type]
        self.assertEqual(outcome.status, "awaiting_input")

    def test_lifecycle_abort_marker_interrupts(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "lifecycle", text="turn_aborted: user cancelled",
                   stop_reason="user cancelled"),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].outcome.status, "interrupted")
        self.assertEqual(
            turns[0].outcome.stop_reason, "user cancelled",
        )
        self.assertEqual(
            derive_session_outcome(turns, snapshot.outcome).status, "aborted",
        )

    def test_native_turn_id_kept(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1", turn_id="native-1"),
            _event(2, "assistant_message", text="a1", turn_id="native-1"),
            _event(3, "user_message", text="q2"),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].turn_id, "native-1")
        self.assertEqual(turns[1].turn_id, "turn-0001")

    def test_failed_turn_keeps_error(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="limit hit",
                   error=_err("provider", "You've hit your session limit")),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].outcome.status, "failed")
        self.assertIsNotNone(turns[0].outcome.error)
        derived = derive_session_outcome(turns, snapshot.outcome)
        self.assertEqual(derived.status, "aborted")
        self.assertEqual(derived.error.message, "You've hit your session limit")

    def test_interrupted_turn(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="[Request interrupted by user]",
                   error=_err("aborted", "[Request interrupted by user]")),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].outcome.status, "interrupted")
        self.assertEqual(
            derive_session_outcome(turns, snapshot.outcome).status, "aborted",
        )

    def test_unresolved_calls_stay_unknown(self):
        snapshot = _snapshot(
            [
                _event(1, "user_message", text="q1"),
                _event(2, "tool_call", name="bash", call_id="c1", raw_input={}),
            ],
            outcome=SessionOutcome("unknown", UNKNOWN_EVIDENCE),
        )
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].outcome.status, "unknown")
        derived = derive_session_outcome(turns, snapshot.outcome)
        self.assertEqual(derived.status, "unknown")

    def test_leading_events_form_turn_zero(self):
        snapshot = _snapshot([
            _event(1, "thinking", text="hmm"),
            _event(2, "user_message", text="q1"),
            _event(3, "assistant_message", text="a1"),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(len(turns), 2)
        self.assertIsNone(turns[0].user_seq)
        self.assertEqual(turns[0].start_seq, 1)

    def test_recovered_mid_turn_error_stays_completed(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="timeout, retrying",
                   error=_err("error", "connection timeout")),
            _event(3, "assistant_message", text="done after retry"),
        ])
        turns = derive_turns(snapshot)
        self.assertEqual(turns[0].outcome.status, "completed")
        self.assertEqual(
            derive_session_outcome(turns, snapshot.outcome).status, "done",
        )

    def test_empty_snapshot_has_no_turns(self):
        snapshot = ActivitySnapshot(
            "empty", (), SessionOutcome("unknown", UNKNOWN_EVIDENCE),
        )
        self.assertEqual(derive_turns(snapshot), ())
        self.assertEqual(
            derive_session_outcome((), snapshot.outcome), snapshot.outcome,
        )

    def test_with_turn_ids_stamps_events(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "assistant_message", text="a1"),
            _event(3, "user_message", text="q2"),
        ])
        stamped = with_turn_ids(snapshot)
        self.assertEqual(
            [e.turn_id for e in stamped.events],
            ["turn-0000", "turn-0000", "turn-0001"],
        )
        # v1 projection input unchanged: same seqs, texts, and count.
        self.assertEqual(
            [(e.seq, e.type, e.text) for e in stamped.events],
            [(e.seq, e.type, e.text) for e in snapshot.events],
        )

    def test_snapshot_accessors(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q1"),
            _event(2, "tool_call", name="bash", call_id="c1", raw_input={}),
            _event(3, "tool_result", call_id="c1", raw_output="ok",
                   result=ToolResultOutcome("ok", NATIVE)),
        ])
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(len(snapshot.invocations), 1)
        self.assertEqual(snapshot.invocations[0].status, "succeeded")
        self.assertEqual(snapshot.invocations[0].pairing, "native")


class PairInvocationsTest(unittest.TestCase):
    def test_native_pairing(self):
        snapshot = _snapshot([
            _event(1, "tool_call", name="bash", call_id="c1", raw_input={}),
            _event(2, "tool_result", call_id="c1", raw_output="ok",
                   result=ToolResultOutcome("ok", NATIVE)),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "succeeded")
        self.assertEqual(invocation.pairing, "native")
        self.assertIsNotNone(invocation.call)
        self.assertIsNotNone(invocation.result)

    def test_failed_result(self):
        snapshot = _snapshot([
            _event(1, "tool_call", name="bash", call_id="c1", raw_input={}),
            _event(2, "tool_result", call_id="c1", raw_output="boom",
                   result=ToolResultOutcome("error", NATIVE)),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "failed")
        self.assertEqual(invocation.pairing, "native")

    def test_unpaired_call_is_proposed(self):
        snapshot = _snapshot([
            _event(1, "tool_call", name="bash", call_id="c9", raw_input={}),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "proposed")
        self.assertEqual(invocation.pairing, "unknown")
        self.assertIsNone(invocation.result)

    def test_empty_id_result_pairs_by_adjacency(self):
        snapshot = _snapshot([
            _event(1, "tool_call", name="bash", call_id="", raw_input={}),
            _event(2, "tool_result", call_id="", raw_output="ok",
                   result=ToolResultOutcome("ok", NATIVE)),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "succeeded")
        self.assertEqual(invocation.pairing, "inferred")

    def test_orphan_result_surfaces_unknown(self):
        snapshot = _snapshot([
            _event(1, "tool_result", call_id="ghost", raw_output="ok",
                   result=ToolResultOutcome("ok", NATIVE)),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "succeeded")
        self.assertEqual(invocation.pairing, "unknown")
        self.assertIsNone(invocation.call)

    def test_denied_comes_only_from_interaction(self):
        denied_request = InteractionRequest(
            "permission", NATIVE, resolution="denied",
            resolution_evidence=NATIVE,
        )
        snapshot = _snapshot([
            _event(1, "tool_call", name="edit", call_id="c1", raw_input={},
                   interaction=denied_request),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "denied")

    def test_timed_out_needs_error_plus_markers(self):
        snapshot = _snapshot([
            _event(1, "tool_call", name="bash", call_id="c1", raw_input={}),
            _event(2, "tool_result", call_id="c1", raw_output="request timed out",
                   result=ToolResultOutcome("error", NATIVE)),
        ])
        (invocation,) = pair_invocations(snapshot)
        self.assertEqual(invocation.status, "timed_out")


class TypedEventTest(unittest.TestCase):
    def test_converter_per_kind(self):
        events = [
            _event(1, "user_message", text="hi"),
            _event(2, "assistant_message", text="hello"),
            _event(3, "thinking", text="hmm"),
            _event(4, "tool_call", name="bash", call_id="c1", raw_input={}),
            _event(5, "tool_result", call_id="c1", raw_output="ok",
                   result=ToolResultOutcome("ok", NATIVE)),
        ]
        typed = [as_typed(event) for event in events]
        self.assertEqual(
            [t.kind for t in typed],
            ["user_message", "assistant_message", "thinking", "tool_call", "tool_result"],
        )
        self.assertTrue(all(t.seq == e.seq for t, e in zip(typed, events)))
        self.assertTrue(all(t.evidence is e.evidence for t, e in zip(typed, events)))
        self.assertEqual(typed[3].name, "bash")

    def test_error_views_classify(self):
        snapshot = _snapshot([
            _event(1, "user_message", text="q"),
            _event(2, "assistant_message", text="429 slow down",
                   error=_err("APIError", "429: slow down", code="429")),
        ])
        (view,) = error_views(snapshot)
        self.assertEqual(view.error_kind, "rate_limited")
        self.assertEqual(view.source_seq, 2)

    def test_error_views_empty_without_errors(self):
        snapshot = _snapshot([_event(1, "user_message", text="q")])
        self.assertEqual(error_views(snapshot), ())


class RealHistoryParityTest(unittest.TestCase):
    """Real-history counts only: turns, outcome agreement, error kinds."""

    def test_real_history_counts(self):
        from sesskit.activity import load_activity
        from sesskit.parsers import claude, codex, cursor, opencode, pi

        scanners = {
            "claude": claude.scan_sessions,
            "codex": codex.scan_sessions,
            "opencode": opencode.scan_sessions,
            "cursor": cursor.scan_sessions,
            "pi": pi.scan_sessions,
        }
        sessions = []
        for runtime, scan in scanners.items():
            try:
                sessions.extend(scan(limit=200))
            except Exception:  # noqa: BLE001, S112 — one runtime scan must not block the rest
                continue
        if not sessions:
            self.skipTest("no local sessions")
        from collections import Counter

        turn_counts: Counter[str] = Counter()
        agreement: Counter[str] = Counter()
        error_kinds: Counter[str] = Counter()
        checked = 0
        for session in sessions:
            runtime = str(session.get("source") or "")
            if runtime == "kimi":
                continue  # Deferred by scope decision.
            try:
                snapshot = load_activity(session)
            except Exception:  # noqa: BLE001, S112 — one unreadable history must not block the rest
                continue
            if snapshot.state != "available":
                continue
            turns = derive_turns(snapshot, runtime=runtime)
            turn_counts[runtime] += len(turns)
            derived = derive_session_outcome(turns, snapshot.outcome)
            agreement[(runtime, snapshot.outcome.status, derived.status)] += 1
            for view in error_views(snapshot, runtime=runtime):
                error_kinds[(runtime, view.error_kind)] += 1
            checked += 1
        print(f"\nchecked={checked} turn_counts={dict(turn_counts)}")
        print(f"agreement={dict(agreement)}")
        print(f"error_kinds={dict(error_kinds)}")
        self.assertGreater(checked, 0, "expected at least one loadable session")


if __name__ == "__main__":
    unittest.main()
