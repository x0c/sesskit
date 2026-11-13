"""Antigravity adapter: CLI/desktop transcripts plus conversation databases."""

from __future__ import annotations

from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    call_scan,
    require_history_file,
    session_path,
)
from sesskit.parsers import antigravity as _parser


class AntigravityAdapter(RuntimeAdapter):
    id = "antigravity"
    display_name = "Antigravity"
    capabilities = Capabilities(
        typed_activity=True,
        incremental_reading=True,
        structured_questions=True,
        native_tool_result_status=True,
        native_turn_boundaries=False,
        conversation_include_errors=True,
        subagent_linkage=False,
        usage_data=False,
        compaction_markers=True,
        message_origin=True,
    )

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False, host: Any = None) -> Any:
        if host is not None:
            return _parser.scan_sessions(
                limit=limit,
                keep_ids=keep_ids,
                include_missing_cwd=include_missing_cwd,
                host=host,
            )
        return call_scan(_parser.scan_sessions, limit, keep_ids,
                         include_missing_cwd=include_missing_cwd)

    def refresh_session(self, session: dict, *, host: Any = None) -> Any:
        return _parser.refresh_session(session, host=host)

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
        from sesskit.activity_antigravity import load_antigravity_activity

        return load_antigravity_activity(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.activity import to_v1_dicts

        snapshot = self.load_activity(session)
        if snapshot.state != "available":
            return []
        return to_v1_dicts(snapshot)

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        from sesskit.activity_reader import open_activity_reader

        return open_activity_reader(session, cursor)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = AntigravityAdapter()
