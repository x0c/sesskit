"""Pi adapter: JSONL message tree with incremental reading."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    call_scan,
    require_history_file,
    session_path,
)
from sesskit.parsers import pi as _parser


def _with_performance(base: Capabilities) -> Capabilities:
    """Attach the performance flag once the core worker's field lands."""
    try:
        return replace(base, performance_data=True)
    except TypeError:
        return base


class PiAdapter(RuntimeAdapter):
    id = "pi"
    display_name = "Pi"
    capabilities = _with_performance(
        Capabilities(
            typed_activity=True,
            incremental_reading=True,
            native_tool_result_status=True,
            native_turn_boundaries=True,
            conversation_include_errors=True,
            subagent_linkage=True,
            usage_data=True,
            compaction_markers=True,
            message_origin=True,
        )
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
        require_history_file(path)
        return project_session_conversation(
            self.load_activity(session),
            include_errors=include_errors,
            history_ref=path,
        )

    def load_activity(self, session: dict) -> Any:
        from sesskit.activity import _load_pi

        return _load_pi(session)

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_pi

        return _parse_pi(session)

    def load_usage(self, session: dict, *, history_view: str = "all_branches") -> Any:
        from sesskit import usage as _usage

        runtime = str(session.get("source") or self.id)
        session_id = str(session.get("id") or "")
        session_ref = f"{runtime}:{session_id}" if session_id else None
        path = str(session.get("path") or "")
        try:
            observations, entries_read, watermark, diagnostics = _usage._load_pi_usage(
                path, history_view, session_id or None)
        except OSError:
            observations, entries_read, watermark, diagnostics = [], 0, None, ()
        return _usage.assemble_snapshot(
            runtime=runtime,
            session_ref=session_ref,
            history_view=history_view,
            observations=observations,
            entries_read=entries_read,
            watermark=watermark,
            extra_diagnostics=diagnostics,
        )

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        from sesskit.activity_reader import open_activity_reader

        return open_activity_reader(session, cursor)

    def signature(self) -> Any:
        return _parser.scan_signature()

    def performance_capabilities(self) -> Any:
        from sesskit.performance_adapters import pi as _pi_performance

        return _pi_performance.get_capabilities()

    def load_performance(self, session: dict, *, history_view: str = "all_branches") -> Any:
        from sesskit.performance_adapters import pi as _pi_performance

        return _pi_performance.load(session, history_view=history_view)


adapter = PiAdapter()
