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
    capabilities = Capabilities()

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

    def signature(self) -> Any:
        return _parser.scan_signature()


adapter = KimiAdapter()
