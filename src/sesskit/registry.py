"""Runtime parser registry — scan/list without launch or handoff."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sesskit.adapters import get_adapter, list_adapters
from sesskit.models import ConversationMessage, SessionInfo

ScanFn = Callable[..., list[SessionInfo]]
LoadFn = Callable[..., list[ConversationMessage]]
SigFn = Callable[[], object | None]


class ConversationLoadError(RuntimeError):
    """History path missing / unreadable, or runtime loader rejected the session."""


def load_session_conversation(session: dict, *, include_errors: bool = False) -> list[ConversationMessage]:
    """Load plain user/assistant turns for a scanned session dict.

    Parser modules keep path-based (or OpenCode db+id) signatures for
    direct callers; this adapter is the public session-dict entry used by the CLI
    and ``RuntimeParser.load_conversation``. ``include_errors`` is currently
    honored by the Pi loader only (error-only turns); other runtimes keep
    their own error policy. Dispatch goes through the runtime adapter
    registry; per-runtime path validation lives in each adapter.
    """
    runtime_id = str(session.get("source") or "")
    try:
        adapter = get_adapter(runtime_id)
    except KeyError:
        raise ConversationLoadError(f"unregistered runtime: {runtime_id or '?'}") from None
    return adapter.load_conversation(session, include_errors=include_errors)


def refresh_session(session: dict, *, host: object | None = None) -> SessionInfo | None:
    """Re-derive one already listed session from its native history.

    Returns the record the next ``scan_sessions`` would produce for the same
    history bytes (``status_tag``, ``completion_id``, excerpts), so a
    consumer can follow one hot session between list scans without
    rescanning every runtime. List membership filters and liveness are not
    applied: the caller owns ``live``/``pid``. None for an unknown runtime,
    a missing history, or a history that no longer yields a session.
    """
    try:
        adapter = get_adapter(str(session.get("source") or ""))
    except KeyError:
        return None
    return adapter.refresh_session(session, host=host)


@dataclass
class RuntimeParser:
    id: str
    display_name: str
    _scan: ScanFn
    _load: LoadFn
    _signature: SigFn | None = None
    adapter: Any | None = None

    def scan_sessions(
        self,
        limit: int = 50,
        keep_ids: set[str] | None = None,
        *,
        include_missing_cwd: bool = False,
        host: object | None = None,
    ) -> list[SessionInfo]:
        if self.adapter is not None:
            try:
                params = inspect.signature(self.adapter.scan).parameters
            except (TypeError, ValueError):
                params = {}
            if host is not None and "host" in params:
                return self.adapter.scan(
                    limit, keep_ids, include_missing_cwd=include_missing_cwd, host=host
                )
            return self.adapter.scan(limit, keep_ids, include_missing_cwd=include_missing_cwd)
        # Parser modules use keyword ``limit`` (positional arg 0 is cwd_filter).
        params = inspect.signature(self._scan).parameters
        kwargs: dict = {"limit": limit}
        if keep_ids is not None and "keep_ids" in params:
            kwargs["keep_ids"] = keep_ids
        if include_missing_cwd and "include_missing_cwd" in params:
            kwargs["include_missing_cwd"] = True
        if host is not None and "host" in params:
            kwargs["host"] = host
        return self._scan(**kwargs)

    def refresh_session(self, session: dict, *, host: object | None = None) -> SessionInfo | None:
        """Re-derive one listed session from its native history (see ``refresh_session``)."""
        if self.adapter is None:
            return None
        payload = session if session.get("source") == self.id else {**session, "source": self.id}
        return self.adapter.refresh_session(payload, host=host)

    def load_conversation(self, session: dict, *, include_errors: bool = False) -> list[ConversationMessage]:
        """Accept a session dict; adapt to path-based parser loaders.

        Test doubles may still register a ``_load(session)`` callable — detected by
        the first parameter name so smoke tests stay simple. ``include_errors``
        is forwarded to loaders that honor it (currently Pi only).
        When this parser wraps a runtime adapter, dispatch goes through it.
        """
        if self.adapter is not None:
            # Keep source aligned with this parser when callers omit/mismatch it.
            payload = session if session.get("source") == self.id else {**session, "source": self.id}
            return self.adapter.load_conversation(payload, include_errors=include_errors)
        try:
            first = next(iter(inspect.signature(self._load).parameters))
        except (StopIteration, TypeError, ValueError):
            first = "path"

        if first in {"session", "session_info", "info"}:
            return self._load(session)

        # Prefer the shared session-dict entry for known runtimes
        # (existence checks + OpenCode id); it dispatches via adapters.
        if self.id in {"claude", "codex", "opencode", "kimi", "cursor", "pi"}:
            # Keep source aligned with this parser when callers omit/mismatch it.
            payload = session if session.get("source") == self.id else {**session, "source": self.id}
            return load_session_conversation(payload, include_errors=include_errors)

        if self.id == "opencode" or len(inspect.signature(self._load).parameters) >= 2:
            return self._load(str(session.get("path") or ""), str(session.get("id") or ""))
        return self._load(str(session.get("path") or ""))

    def scan_signature(self) -> object | None:
        if self.adapter is not None:
            return self.adapter.signature()
        if self._signature is None:
            return None
        return self._signature()


class ParserRegistry:
    def __init__(self, runtimes: list[RuntimeParser]):
        self._runtimes = {r.id: r for r in runtimes}
        self._last_scan_errors: dict[str, str] = {}
        if not self._runtimes:
            raise ValueError("at least one runtime parser is required")

    def __iter__(self):
        return iter(self._runtimes.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._runtimes)

    def get(self, runtime_id: str) -> RuntimeParser:
        try:
            return self._runtimes[runtime_id]
        except KeyError as exc:
            raise KeyError(f"unregistered runtime: {runtime_id}") from exc

    def scan_all(
        self,
        limit: int,
        *,
        include_missing_cwd: bool = False,
        raise_on_scan_error: bool = False,
        host: object | None = None,
    ) -> dict[str, list[SessionInfo]]:
        runtimes = list(self)

        def _one(runtime: RuntimeParser) -> tuple[str, list[SessionInfo], str | None]:
            try:
                return (
                    runtime.id,
                    runtime.scan_sessions(
                        limit,
                        include_missing_cwd=include_missing_cwd,
                        host=host,
                    ),
                    None,
                )
            except Exception as exc:  # noqa: BLE001 — isolate one runtime failure
                return runtime.id, [], f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
            scanned = list(pool.map(_one, runtimes))
        result: dict[str, list[SessionInfo]] = {}
        errors: dict[str, str] = {}
        for runtime_id, sessions, err in scanned:
            result[runtime_id] = sessions
            if err:
                errors[runtime_id] = err
        self._last_scan_errors = errors
        if raise_on_scan_error and errors:
            detail = "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
            raise RuntimeError(f"session scan failed: {detail}")
        return result

    @property
    def last_scan_errors(self) -> dict[str, str]:
        return dict(self._last_scan_errors)


def default_registry() -> ParserRegistry:
    """Build the registry from the runtime adapter set.

    Each ``RuntimeParser`` wraps its adapter; scan, conversation load, and
    signature dispatch go through the adapter only.
    """
    return ParserRegistry(
        [
            RuntimeParser(
                id=adapter.id,
                display_name=adapter.display_name,
                _scan=adapter.scan,
                _load=adapter.load_conversation,
                _signature=adapter.signature,
                adapter=adapter,
            )
            for adapter in list_adapters()
        ]
    )
