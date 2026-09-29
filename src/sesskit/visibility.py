"""One shared visibility predicate for chat-readable projections.

What is hidden from plain conversation — injected/system context and
error-only turns — is decided here once and used by both the conversation
projection (``conversation.py``) and the v1 projection (``activity.py``
``to_v1_dicts``), so the two can never drift apart again.
"""

from __future__ import annotations

from sesskit.models import ActivityEvent


def visible_in_v1(event: ActivityEvent) -> bool:
    """Whether ``to_v1_dicts`` projects this event.

    ``compaction`` boundary events and injected-context user messages never
    appeared in v1; everything else projects (typed-only ``lifecycle``
    markers are skipped the same way).
    """
    if event.type in {"compaction", "lifecycle"}:
        return False
    return not (event.type == "user_message" and event.origin == "injected")


def visible_in_conversation(event: ActivityEvent, *, include_errors: bool = False) -> bool:
    """Whether the plain-conversation projection shows this event.

    Thinking, tool calls/results, compaction, and lifecycle markers never
    enter plain conversation. Injected (non-human) user rows are skipped.
    An assistant turn whose text is only a surfaced native error is hidden
    by default and returned only with ``include_errors=True``; assistant
    text carrying error evidence alongside genuine reply content is shown.
    """
    if event.type == "user_message":
        if event.origin == "injected":
            return False
        return isinstance(event.text, str) and bool(event.text.strip())
    if event.type == "assistant_message":
        if not (isinstance(event.text, str) and event.text.strip()):
            return False
        return not (
            not include_errors
            and event.error is not None
            and event.text == event.error.message
        )
    return False
