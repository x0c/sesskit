"""OpenCode adapter: single SQLite database, v1 plus v2 table families."""

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
from sesskit.parsers import opencode as _parser


class OpenCodeAdapter(RuntimeAdapter):
    id = "opencode"
    display_name = "OpenCode"
    capabilities = Capabilities(
        typed_activity=True,
        incremental_reading=True,
        structured_questions=True,
        native_tool_result_status=True,
        native_turn_boundaries=True,
        conversation_include_errors=True,
        subagent_linkage=True,
        usage_data=True,
        compaction_markers=True,
        message_origin=True,
        performance_data=True,
    )

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
        return call_scan(_parser.scan_sessions, limit, keep_ids,
                         include_missing_cwd=include_missing_cwd)

    def refresh_session(self, session: dict, *, host: Any = None) -> Any:
        return _parser.refresh_session(session, host=host)

    def load_conversation(self, session: dict, *, include_errors: bool = False) -> Any:
        from sesskit.conversation import project_session_conversation

        path = session_path(session, self.id)
        session_id = str(session.get("id") or "")
        if not session_id:
            raise _load_error("opencode session is missing id")
        if not os.path.exists(path):
            raise _load_error(f"history database not found: {path}")
        return project_session_conversation(
            self.load_activity(session),
            include_errors=include_errors,
            history_ref=path,
        )

    def load_activity(self, session: dict) -> Any:
        from sesskit.activity import _load_opencode_legacy_schema
        from sesskit.activity_opencode import load_opencode_activity

        snapshot = load_opencode_activity(session)
        if snapshot.state != "unsupported":
            return snapshot
        return _load_opencode_legacy_schema(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_opencode

        return _parse_opencode(session)

    def load_usage(self, session: dict, *, history_view: str = "all_branches") -> Any:
        from sesskit import usage as _usage

        runtime = str(session.get("source") or self.id)
        session_id = str(session.get("id") or "")
        session_ref = f"{runtime}:{session_id}" if session_id else None
        path = str(session.get("path") or "")
        diagnostics: list[str] = []
        if history_view == "active_branch":
            diagnostics.append("branch_views_unsupported_fallback_all_branches")
        try:
            observations, _rollup, entries_read, watermark = _usage._load_opencode_usage(
                path, session_id)
        except OSError:
            observations, entries_read, watermark = [], 0, None
        return _usage.assemble_snapshot(
            runtime=runtime, session_ref=session_ref, history_view=history_view,
            observations=observations, entries_read=entries_read,
            watermark=watermark,
            extra_diagnostics=tuple(diagnostics))

    def performance_capabilities(self) -> Any:
        from sesskit.performance_adapters import opencode as _performance

        return _performance.CAPABILITIES

    def load_performance(self, session: dict, *,
                         history_view: str = "all_branches") -> Any:
        from sesskit.performance_adapters import opencode as _performance

        return _performance.load(session, history_view=history_view)

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        from sesskit.activity_reader import open_activity_reader

        return open_activity_reader(session, cursor)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = OpenCodeAdapter()
