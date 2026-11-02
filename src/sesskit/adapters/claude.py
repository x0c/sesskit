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

_CAPABILITY_FLAG_KWARGS = {
    "typed_activity": True,
    "incremental_reading": True,
    "structured_questions": True,
    "native_tool_result_status": True,
    "native_turn_boundaries": True,
    "conversation_include_errors": True,
    "subagent_linkage": True,
    "usage_data": True,
    "compaction_markers": True,
    "message_origin": True,
}


class ClaudeAdapter(RuntimeAdapter):
    id = "claude"
    display_name = "Claude Code"
    try:
        # Owned by the core worker (Capabilities.performance_data); falls
        # back until that field lands so this module stays import-safe.
        capabilities = Capabilities(performance_data=True, **_CAPABILITY_FLAG_KWARGS)
    except TypeError:
        capabilities = Capabilities(**_CAPABILITY_FLAG_KWARGS)

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
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
        from sesskit.activity import _load_claude

        return _load_claude(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_claude

        return _parse_claude(session)

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
            observations, entries_read, watermark, read_diagnostics = _usage._load_jsonl_usage(
                path, "claude", session_id or None)
            diagnostics.extend(read_diagnostics)
        except OSError:
            observations, entries_read, watermark = [], 0, None
        return _usage.assemble_snapshot(
            runtime=runtime, session_ref=session_ref, history_view=history_view,
            observations=observations, entries_read=entries_read,
            watermark=watermark,
            extra_diagnostics=tuple(diagnostics))

    def load_performance(self, session: dict, *, history_view: str = "all_branches") -> Any:
        from sesskit.performance_adapters import claude as _performance_claude

        return _performance_claude.load(session, history_view=history_view)

    def performance_capabilities(self) -> Any:
        from sesskit.performance_adapters import claude as _performance_claude

        return _performance_claude.CAPABILITIES

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        from sesskit.activity_reader import open_activity_reader

        return open_activity_reader(session, cursor)

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = ClaudeAdapter()
