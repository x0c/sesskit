"""Kimi adapter: workspace session dirs with wire protocol log.

Typed-activity adaptation is deferred by explicit scope decision; Kimi
reports ``unsupported`` from ``load_activity`` and keeps its dedicated
transcript parser.
"""

from __future__ import annotations

from typing import Any

from sesskit.adapters.base import (
    Capabilities,
    RuntimeAdapter,
    call_scan,
    require_history_file,
    session_path,
)
from sesskit.models import ActivitySnapshot, Evidence, SessionOutcome
from sesskit.parsers import kimi as _parser


class KimiAdapter(RuntimeAdapter):
    id = "kimi"
    display_name = "Kimi Code"
    # Tail-role inference only (no explicit native turn markers surfaced),
    # heuristic tool-result status, no structured interaction normalization.
    # Typed-activity adaptation stays deferred, but the wire log's
    # turn-scoped `usage.record` events are a verified native usage mapping.
    capabilities = Capabilities(usage_data=True, performance_data=True)

    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
        return call_scan(_parser.scan_sessions, limit, keep_ids,
                         include_missing_cwd=include_missing_cwd)

    def refresh_session(self, session: dict, *, host: Any = None) -> Any:
        return _parser.refresh_session(session, host=host)

    def load_conversation(self, session: dict, *, include_errors: bool = False) -> Any:
        path = session_path(session, self.id)
        require_history_file(path)
        return _parser.load_conversation(path)

    def load_activity(self, session: dict) -> Any:
        return ActivitySnapshot(
            state="unsupported",
            events=(),
            outcome=SessionOutcome("unknown", Evidence("unknown")),
        )

    def load_events(self, session: dict) -> list[dict]:
        from sesskit.transcript import _parse_kimi

        return _parse_kimi(session)

    def performance_capabilities(self) -> Any:
        from sesskit.performance_adapters import kimi as _performance_kimi

        return _performance_kimi.CAPABILITIES

    def load_performance(self, session: dict, *,
                         history_view: str = "all_branches") -> Any:
        from sesskit.performance_adapters import kimi as _performance_kimi

        return _performance_kimi.load(session, history_view=history_view)

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
                path, "kimi", session_id or None)
            diagnostics.extend(read_diagnostics)
        except OSError:
            observations, entries_read, watermark = [], 0, None
        return _usage.assemble_snapshot(
            runtime=runtime, session_ref=session_ref, history_view=history_view,
            observations=observations, entries_read=entries_read,
            watermark=watermark,
            extra_diagnostics=tuple(diagnostics))

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = KimiAdapter()
