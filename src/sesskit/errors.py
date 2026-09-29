"""Closed error taxonomy over native failure evidence (stage C).

Maps the free-text ``AgentError`` values producers extract from native
history (native error names, HTTP status codes, human-readable messages)
to the closed :data:`~sesskit.models.ErrorKind` set. Classification uses
only evidence present on the error; anything unclassifiable stays
``unknown``, never guessed.
"""

from __future__ import annotations

import re

from sesskit.models import AgentError, ErrorKind

_RETRYABLE: dict[str, bool | None] = {
    "rate_limited": True,
    "quota_exhausted": False,
    "auth": False,
    "context_length": False,
    "provider_overloaded": True,
    "provider_error": True,
    "policy_blocked": False,
    "tool_error": False,
    "user_interrupt": False,
    "timeout": True,
    "runtime_crash": None,
    "unknown": None,
}

_INTERRUPT_MARKERS = (
    "interrupted by user",
    "request interrupted",
    "user interrupt",
    "cancelled by user",
    "canceled by user",
    "user cancelled",
    "user canceled",
    "turn_aborted",
    "messageabortederror",
    "operation was aborted",
    "user_abort",
)

_INTERRUPT_KINDS = frozenset({
    "aborted", "abort", "interrupt", "interrupted", "user_interrupt",
    "turn_aborted", "messageabortederror",
})

_QUOTA_MARKERS = (
    "usage_limit_exceeded",
    "usage limit",
    "quota",
    "session limit",
    "session_limit",
    "insufficient credits",
    "insufficient_quota",
    "credit balance",
    "credits exhausted",
    "weekly limit",
    "plan limit",
    "billing",
)

_RATE_MARKERS = (
    "rate limit",
    "rate_limit",
    "rate-limited",
    "too many requests",
    "429",
)

_AUTH_MARKERS = (
    "providerautherror",
    "provider_auth",
    "unauthorized",
    "unauthenticated",
    "invalid api key",
    "invalid_api_key",
    "api key is invalid",
    "api_key_invalid",
    "authentication",
    "forbidden",
    "access denied",
)

_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "contextlimit",
    "maximum context",
    "max tokens",
    "max_tokens",
    "output length",
    "messageoutputlengtherror",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "prompt too long",
)

_OVERLOAD_MARKERS = (
    "overloaded",
    "overload",
    "capacity",
    "529",
    "server is busy",
)

_TOOL_MARKERS = (
    "tool_error",
    "tool failed",
    "tool failure",
    "tool execution failed",
    "command failed",
    "exit code",
    "is_error",
    "permission denied",
    "eacces",
)

_POLICY_MARKERS = (
    "policy_blocked",
    "blocked by policy",
    "blocked by content policy",
    "content policy",
    "policy violation",
    "violates policy",
    "guardrail",
    "safety system",
    "content_filter",
    "content filter",
)

_TIMEOUT_MARKERS = (
    "timed_out",
    "timed out",
    "timeout",
    "deadline exceeded",
    "deadline_exceeded",
    "etimedout",
    "request took too long",
)

#: Refusals and denials are never errors: text matching these markers stays
#: ``unknown`` at the error-taxonomy level and surfaces through interaction
#: resolution / invocation status instead.
_REFUSAL_MARKERS = (
    "refus",
    "denied by user",
    "declined by user",
    "rejected by user",
    "not approved",
    "did not approve",
    "permission denied by user",
)

_CRASH_MARKERS = (
    "runtime_crash",
    "crashed",
    "segfault",
    "segmentation fault",
    "out of memory",
    "oom-killed",
    "process died",
    "was killed",
)


def _has_code(text: str, code: str) -> bool:
    return f" {code} " in f" {text} " or text.startswith(code + " ") or text.endswith(" " + code)


def native_http_status(*parts: object) -> int | None:
    """First native HTTP status (100-599) found across the given raw values.

    Only values natively present are returned; nothing is inferred. Used to
    populate ``AgentError.http_status`` from native codes/messages.
    """
    for part in parts:
        if part is None or isinstance(part, bool):
            continue
        text = str(part).strip()
        if not text:
            continue
        if re.fullmatch(r"\d{3}", text) and 100 <= int(text) <= 599:
            return int(text)
        match = re.search(r"(?<![\d])((?:[1-5])\d{2})(?![\d])", text)
        if match:
            return int(match.group(1))
    return None


def classify_error(
    *,
    runtime: str = "",
    kind: str = "",
    code: str | int | None = "",
    message: str = "",
) -> tuple[ErrorKind, bool | None]:
    """Classify native failure evidence into ``(ErrorKind, retryable)``.

    Inputs are the raw ``AgentError`` fields plus the runtime id for
    context. ``retryable`` is True/False/None (unknown). Unclassifiable
    input returns ``("unknown", None)``.
    """
    _ = runtime  # Reserved: per-runtime precedence if evidence ever conflicts.
    raw_kind = str(kind or "").strip()
    raw_code = "" if code is None or isinstance(code, bool) else str(code).strip()
    text = f"{raw_kind} {raw_code} {message or ''}".lower()
    compact = text.replace("_", "").replace("-", "").replace(" ", "")

    # Refusals and denials are never errors, even when worded like failures.
    if any(marker in text for marker in _REFUSAL_MARKERS):
        return "unknown", _RETRYABLE["unknown"]
    if any(marker in text for marker in _INTERRUPT_MARKERS) or raw_kind.lower() in _INTERRUPT_KINDS:
        return "user_interrupt", _RETRYABLE["user_interrupt"]
    if (
        raw_code.lower() == "usage_limit_exceeded"
        or any(marker in text for marker in _QUOTA_MARKERS)
    ):
        return "quota_exhausted", _RETRYABLE["quota_exhausted"]
    if any(marker in text for marker in _RATE_MARKERS):
        return "rate_limited", _RETRYABLE["rate_limited"]
    if (
        _has_code(text, "401")
        or _has_code(text, "403")
        or any(marker in text for marker in _AUTH_MARKERS)
    ):
        return "auth", _RETRYABLE["auth"]
    if any(marker in text for marker in _CONTEXT_MARKERS):
        return "context_length", _RETRYABLE["context_length"]
    if any(marker in text for marker in _OVERLOAD_MARKERS):
        return "provider_overloaded", _RETRYABLE["provider_overloaded"]
    if any(marker in text for marker in _POLICY_MARKERS):
        return "policy_blocked", _RETRYABLE["policy_blocked"]
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout", _RETRYABLE["timeout"]
    if any(marker in text for marker in _TOOL_MARKERS):
        return "tool_error", _RETRYABLE["tool_error"]
    if any(marker in text for marker in _CRASH_MARKERS):
        return "runtime_crash", _RETRYABLE["runtime_crash"]
    # Legacy producer shorthands stay reachable: bare "provider" means a
    # provider-side failure with no finer evidence; bare "error" defers to
    # the message rules above and falls through to unknown here.
    if (raw_kind.lower() in {"provider", "provider_error"}
            or "apierror" in compact or "providererror" in compact):
        return "provider_error", _RETRYABLE["provider_error"]
    for http in ("500", "502", "503", "504", "522", "524"):
        if _has_code(text, http):
            return "provider_error", _RETRYABLE["provider_error"]
    if any(
        marker in text
        for marker in (
            "internal error", "server error", "upstream error", "provider error",
            "connection", "econnreset", "etimedout", "timeout", "timed out",
            "temporarily unavailable", "bad gateway", "service unavailable",
            "bad_request", "invalid-request", "invalid_request",
            "invalid-output", "invalid_output", "invalid output",
            "certificate", "tls handshake", "ssl error",
        )
    ):
        return "provider_error", _RETRYABLE["provider_error"]
    return "unknown", _RETRYABLE["unknown"]


def classify_agent_error(error: AgentError, *, runtime: str = "") -> tuple[ErrorKind, bool | None]:
    """Classify a structured :class:`AgentError` using its present fields."""
    return classify_error(
        runtime=runtime, kind=error.kind, code=error.code, message=error.message,
    )


def with_classification(error: AgentError, *, runtime: str = "") -> AgentError:
    """Return ``error`` with ``retryable`` filled from the taxonomy.

    The original ``kind``/``code``/``message``/``evidence`` are preserved;
    only the advisory ``retryable`` flag is set (overwriting an explicit
    ``None``; a producer-set value is kept).
    """
    _, retryable = classify_agent_error(error, runtime=runtime)
    if error.retryable is not None:
        return error
    return AgentError(
        kind=error.kind,
        message=error.message,
        evidence=error.evidence,
        code=error.code,
        retryable=retryable,
    )
