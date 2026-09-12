"""SessKit: read, parse, and export local coding-agent sessions."""

from __future__ import annotations

__version__ = "0.1.2"

from sesskit.models import ConversationMessage, SessionInfo, make_session_info, session_key
from sesskit.registry import ConversationLoadError, load_session_conversation
from sesskit.transcript import SCHEMA_ID, count_events, load_events

__all__ = [
    "ConversationLoadError",
    "ConversationMessage",
    "SCHEMA_ID",
    "SessionInfo",
    "__version__",
    "count_events",
    "load_events",
    "load_session_conversation",
    "make_session_info",
    "session_key",
]
