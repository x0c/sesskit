"""Plain conversation as a projection of typed activity.

``conversation_from_activity`` is the single interpreter of typed snapshots
for chat-readable ``(role, text)`` turns. Per-runtime ``load_conversation``
parsers are the legacy source; the registry dispatches migrated runtimes here.
v1 transcript output and the CLI envelope are unchanged by this projection.

Unified error policy: an assistant turn whose text is only a surfaced native
error (``event.error`` present and the text equals the error message) is
hidden from the default chat view and returned only with
``include_errors=True``. Assistant text that carries error evidence alongside
genuine reply content is always shown: hiding it would drop real answers.
Runtimes without error evidence (cursor prompt fallback) are unaffected, and
Kimi stays on its legacy parser by explicit scope decision. Injected
(non-human) user rows are skipped, mirroring the v1 projection.
"""

from __future__ import annotations

from sesskit.models import ActivitySnapshot, ConversationMessage
from sesskit.visibility import visible_in_conversation


def conversation_from_activity(
    snapshot: ActivitySnapshot, *, include_errors: bool = False,
) -> list[ConversationMessage]:
    """Project user/assistant turns from a typed activity snapshot.

    Thinking, tool calls, and tool results never enter plain conversation.
    Blank texts are skipped, mirroring the legacy parsers. Timestamps ride
    along when the snapshot provides them; runtimes without native times
    (cursor store blobs, prompt fallback) yield ``None``, as before.
    Visibility itself lives in :mod:`sesskit.visibility`, shared with v1.
    """
    messages: list[ConversationMessage] = []
    for event in snapshot.events:
        if not visible_in_conversation(event, include_errors=include_errors):
            continue
        if event.type == "user_message":
            messages.append(ConversationMessage("user", event.text or "", event.ts))
        elif event.type == "assistant_message":
            messages.append(ConversationMessage("assistant", event.text or "", event.ts))
    return messages


def project_session_conversation(
    snapshot: ActivitySnapshot, *, include_errors: bool = False,
    history_ref: str = "",
) -> list[ConversationMessage]:
    """Adapt a snapshot load to the public conversation contract.

    ``available`` snapshots project to turns; ``empty`` (readable history
    with zero normalized events) yields ``[]``; ``unavailable`` or
    ``unsupported`` histories raise the shared missing-history error rather
    than disguising an unreadable file as an empty conversation.
    """
    if snapshot.state == "available":
        return conversation_from_activity(snapshot, include_errors=include_errors)
    if snapshot.state == "empty":
        return []
    from sesskit.registry import ConversationLoadError

    detail = f": {history_ref}" if history_ref else ""
    raise ConversationLoadError(f"history unreadable{detail}")
