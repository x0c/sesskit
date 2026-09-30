"""Codex CLI adapter: rollout session files plus thread-name index."""

from __future__ import annotations

from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    call_scan,
    require_history_file,
    session_path,
)
from sesskit.parsers import codex as _parser


class CodexAdapter(RuntimeAdapter):
    id = "codex"
    display_name = "Codex CLI"
    # Tool outputs carry no native success/failure flag (heuristic only);
    # turn completion/abort markers are native.
    capabilities = Capabilities(
        typed_activity=True,
        incremental_reading=True,
        structured_questions=True,
        native_tool_result_status=False,
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
        from sesskit.activity import _load_codex

        return _load_codex(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_codex

        return _parse_codex(session)

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        from sesskit.activity_reader import open_activity_reader

        return open_activity_reader(session, cursor)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = CodexAdapter()
