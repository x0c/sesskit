"""Shared base for runtime adapters: capabilities and session-dict validation."""

from __future__ import annotations

import inspect
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


def _load_error(message: str) -> Exception:
    """Build the shared missing-history error without a module cycle."""
    from sesskit.registry import ConversationLoadError

    return ConversationLoadError(message)


@dataclass(frozen=True)
class Capabilities:
    """Typed, per-runtime capability declaration.

    Every flag defaults to False; an adapter sets a flag only when its
    native history provides direct evidence for it. Unobserved or
    unverified behavior stays False, never inferred. Consumers branch on
    these flags, never on runtime names.
    """

    #: Full-snapshot typed activity is available (``load_activity`` returns
    #: ``available``/``empty``/``unavailable`` instead of ``unsupported``).
    typed_activity: bool = False
    #: An incremental reader exists (``open_reader`` works); otherwise
    #: consumers fall back to snapshots.
    incremental_reading: bool = False
    #: Structured question/answer shapes are normalized to interaction
    #: records with grouped options and resolution.
    structured_questions: bool = False
    #: Permission/approval/elicitation records are normalized.
    permission_records: bool = False
    #: Tool results carry a native success/failure marker; otherwise the
    #: status is healed per heuristic evidence and stays ``unknown`` when
    #: evidence is absent.
    native_tool_result_status: bool = False
    #: Native history carries explicit turn-completion markers (stop
    #: reasons, completion/abort events, finish flags) used for outcome
    #: inference. This is not a full turn model.
    native_turn_boundaries: bool = False
    #: Child-agent/subagent linkage is exposed.
    subagent_linkage: bool = False
    #: Model/token usage data is exposed.
    usage_data: bool = False
    #: Native context-compaction boundaries are exposed as typed markers.
    compaction_markers: bool = False
    #: User-message events carry a native-evidence origin
    #: (human/injected/system/unknown) so consumers need no heuristics.
    message_origin: bool = False
    #: Plain conversation load accepts ``include_errors`` to bring back
    #: error-only assistant turns hidden from the default chat view.
    conversation_include_errors: bool = False


def session_path(session: dict, runtime_id: str) -> str:
    """Return the history path or raise the legacy missing-path error."""
    path = str(session.get("path") or "")
    if not path:
        raise _load_error(
            f"session has no history path (runtime={runtime_id or '?'})"
        )
    return path


def require_history_file(path: str) -> None:
    """Raise the legacy unreadable-history error unless ``path`` exists."""
    if not os.path.exists(path):
        raise _load_error(f"history path not found: {path}")


def call_scan(scan_fn: Any, limit: int = 50, keep_ids: set[str] | None = None,
              *, include_missing_cwd: bool = False) -> Any:
    """Invoke a parser ``scan_sessions`` honoring only its accepted params."""
    params = inspect.signature(scan_fn).parameters
    kwargs: dict = {"limit": limit}
    if keep_ids is not None and "keep_ids" in params:
        kwargs["keep_ids"] = keep_ids
    if include_missing_cwd and "include_missing_cwd" in params:
        kwargs["include_missing_cwd"] = True
    return scan_fn(**kwargs)


class RuntimeAdapter(ABC):
    """One adapter per runtime: scan, conversation, activity, and events."""

    id: str = ""
    display_name: str = ""
    capabilities: Capabilities = Capabilities()

    @abstractmethod
    def scan(self, limit: int = 50, keep_ids: set[str] | None = None,
             *, include_missing_cwd: bool = False) -> Any:
        """Scan native history; mirrors the parser ``scan_sessions`` shape."""

    def refresh_session(self, session: dict, *, host: Any = None) -> Any:
        """Re-derive one listed session from its native history; None when gone.

        Same record ``scan`` would produce for that history (status,
        ``completion_id``, excerpts) without list filters or liveness.
        """
        return None

    @abstractmethod
    def load_conversation(self, session: dict, *, include_errors: bool = False) -> Any:
        """Load plain user/assistant turns for a scanned session dict."""

    @abstractmethod
    def load_activity(self, session: dict) -> Any:
        """Load a typed activity snapshot for a scanned session dict."""

    @abstractmethod
    def load_events(self, session: dict) -> list[dict]:
        """Project the session to v1 transcript dicts (byte-compatible)."""

    def open_reader(self, session: dict, cursor: Any = None) -> Any:
        """Open an incremental reader; raises when unsupported."""
        from sesskit.activity_reader import IncrementalUnsupported

        raise IncrementalUnsupported(
            f"incremental reading is unsupported for runtime {self.id!r}; "
            "use load_activity instead",
        )

    @abstractmethod
    def signature(self) -> Any:
        """Cheap list-level version; must stay stable under streaming writes."""
