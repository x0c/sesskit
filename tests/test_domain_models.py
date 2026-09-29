from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from sesskit.models import (
    AgentError,
    Evidence,
    SessionInfo,
    SessionOutcome,
    ToolResultOutcome,
)


def test_evidence_and_error_preserve_native_detail_without_inventing_a_code():
    evidence = Evidence("native", field="error.formatted", record="row-7")
    error = AgentError(kind="provider", message="Request failed", evidence=evidence)

    assert error.code is None
    assert error.evidence == evidence
    with pytest.raises(FrozenInstanceError):
        error.message = "changed"


def test_known_tool_result_requires_known_evidence():
    native = Evidence("native", field="result.status", record="event-4")

    assert ToolResultOutcome("ok", native).status == "ok"
    assert ToolResultOutcome("unknown", Evidence("unknown")).status == "unknown"
    with pytest.raises(ValueError, match="requires native or inferred evidence"):
        ToolResultOutcome("ok", Evidence("unknown"))


def test_session_outcome_never_turns_unknown_evidence_into_success():
    native = Evidence("native", field="stopReason", record="message-3")
    assert SessionOutcome("done", native).status == "done"
    assert SessionOutcome("unknown", Evidence("unknown")).status == "unknown"

    with pytest.raises(ValueError, match="requires native or inferred evidence"):
        SessionOutcome("done", Evidence("unknown"))


def test_completed_session_cannot_carry_an_agent_error():
    evidence = Evidence("native", field="stopReason", record="message-3")
    error = AgentError(
        kind="provider",
        message="Quota exceeded",
        evidence=Evidence("native", field="error.message", record="message-3"),
    )

    with pytest.raises(ValueError, match="cannot carry an agent error"):
        SessionOutcome("done", evidence, error=error)


def test_session_info_types_exclude_corral_only_fields():
    hints = get_type_hints(SessionInfo)

    assert "thread_source" in hints
    assert not {
        "keepalive_name",
        "provisional",
        "attention_kind",
        "attention_token",
        "attention_updated_at",
    } & hints.keys()
