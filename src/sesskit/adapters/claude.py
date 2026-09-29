"""Claude Code adapter: per-project JSONL histories."""

from __future__ import annotations

from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    call_scan,
    require_history_file,
    session_path,
)
from sesskit.parsers import claude as _parser


class ClaudeAdapter(RuntimeAdapter):
    id = "claude"
    display_name = "Claude Code"
    capabilities = Capabilities(
        typed_activity=True,
        structured_questions=True,
        native_tool_result_status=True,
        native_turn_boundaries=True,
        conversation_include_errors=True,
        subagent_linkage=True,
        usage_data=True,
        compaction_markers=True,
        message_origin=True,
    )

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
        return call_scan(_parser.scan_sessions, limit, keep_ids,
                         include_missing_cwd=include_missing_cwd)

    def load_conversation(self, session: dict, *, include_errors: bool = False) -> Any:
        from sesskit.conversation import project_session_conversation

        path = session_path(session, self.id)
        require_history_file(path)
        return project_session_conversation(
            self.load_activity(session),
            include_errors=include_errors,
            history_ref=path,
        )

    def load_activity(self, session: dict) -> Any:
        from sesskit.activity import _load_claude

        return _load_claude(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_claude

        return _parse_claude(session)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = ClaudeAdapter()
