"""Cursor adapter: workspace-hash chat dirs with SQLite store."""

from __future__ import annotations

import os
from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    _load_error,
    call_scan,
    session_path,
)
from sesskit.models import ActivitySnapshot, Evidence, SessionOutcome
from sesskit.parsers import cursor as _parser


class CursorAdapter(RuntimeAdapter):
    id = "cursor"
    display_name = "Cursor Agent"
    # Stored rows carry no reliable turn-completion marker (outcome stays
    # unknown) and no native tool-result status; question shapes are
    # normalized where present.
    capabilities = Capabilities(
        typed_activity=True,
        structured_questions=True,
        native_tool_result_status=False,
        native_turn_boundaries=False,
        conversation_include_errors=True,
        subagent_linkage=True,
        usage_data=False,
        compaction_markers=False,
        message_origin=True,
    )

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
        return call_scan(_parser.scan_sessions, limit, keep_ids,
                         include_missing_cwd=include_missing_cwd)

    def load_conversation(self, session: dict, *, include_errors: bool = False) -> Any:
        from sesskit.conversation import project_session_conversation

        path = session_path(session, self.id)
        # Cursor accepts a chat dir or store.db.
        if not (os.path.isfile(path) or os.path.isdir(path)):
            raise _load_error(f"history path not found: {path}")
        return project_session_conversation(
            self.load_activity(session),
            include_errors=include_errors,
            history_ref=path,
        )

    def load_activity(self, session: dict) -> Any:
        from sesskit.activity_cursor import load_cursor_activity

        path = str(session.get("path") or "")
        if path and not path.endswith("store.db") and not os.path.isdir(path):
            return ActivitySnapshot(
                state="unsupported",
                events=(),
                outcome=SessionOutcome("unknown", Evidence("unknown")),
            )
        return load_cursor_activity(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_cursor

        return _parse_cursor(session)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = CursorAdapter()
