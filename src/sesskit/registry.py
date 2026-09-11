"""Runtime parser registry — scan/list without launch or handoff."""

from __future__ import annotations

import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from sesskit.models import ConversationMessage, SessionInfo
from sesskit.parsers import claude, codex, cursor, kimi, opencode, pi

ScanFn = Callable[..., list[SessionInfo]]
LoadFn = Callable[..., list[ConversationMessage]]
SigFn = Callable[[], object | None]


class ConversationLoadError(RuntimeError):
    """History path missing / unreadable, or runtime loader rejected the session."""


def load_session_conversation(session: dict) -> list[ConversationMessage]:
    """Load plain user/assistant turns for a scanned session dict.

    Parser modules keep path-based (or OpenCode db+id) signatures for Corral and
    direct callers; this adapter is the public session-dict entry used by the CLI
    and ``RuntimeParser.load_conversation``.
    """
    runtime_id = str(session.get("source") or "")
    path = str(session.get("path") or "")
    if not path:
        raise ConversationLoadError(f"session has no history path (runtime={runtime_id or '?'})")

    if runtime_id == "opencode":
        session_id = str(session.get("id") or "")
        if not session_id:
            raise ConversationLoadError("opencode session is missing id")
        if not os.path.exists(path):
            raise ConversationLoadError(f"history database not found: {path}")
        return opencode.load_conversation(path, session_id)

    loaders = {
        "claude": claude.load_conversation,
        "codex": codex.load_conversation,
        "kimi": kimi.load_conversation,
        "cursor": cursor.load_conversation,
        "pi": pi.load_conversation,
    }
    loader = loaders.get(runtime_id)
    if loader is None:
        raise ConversationLoadError(f"unregistered runtime: {runtime_id or '?'}")

    # Cursor accepts a chat dir or store.db; other JSONL parsers need a readable path.
    if runtime_id == "cursor":
        if not (os.path.isfile(path) or os.path.isdir(path)):
            raise ConversationLoadError(f"history path not found: {path}")
    elif not os.path.exists(path):
        raise ConversationLoadError(f"history path not found: {path}")

    return loader(path)


@dataclass
class RuntimeParser:
    id: str
    display_name: str
    _scan: ScanFn
    _load: LoadFn
    _signature: SigFn | None = None

    def scan_sessions(
        self,
        limit: int = 50,
        keep_ids: set[str] | None = None,
        *,
        include_missing_cwd: bool = False,
    ) -> list[SessionInfo]:
        # Parser modules use keyword ``limit`` (positional arg 0 is cwd_filter).
        params = inspect.signature(self._scan).parameters
        kwargs: dict = {"limit": limit}
        if keep_ids is not None and "keep_ids" in params:
            kwargs["keep_ids"] = keep_ids
        if include_missing_cwd and "include_missing_cwd" in params:
            kwargs["include_missing_cwd"] = True
        return self._scan(**kwargs)

    def load_conversation(self, session: dict) -> list[ConversationMessage]:
        """Accept a session dict; adapt to path-based parser loaders.

        Test doubles may still register a ``_load(session)`` callable — detected by
        the first parameter name so smoke tests stay simple.
        """
        try:
            first = next(iter(inspect.signature(self._load).parameters))
        except (StopIteration, TypeError, ValueError):
            first = "path"

        if first in {"session", "session_info", "info"}:
            return self._load(session)

        # Prefer the shared adapter for known runtimes (existence checks + OpenCode id).
        if self.id in {"claude", "codex", "opencode", "kimi", "cursor", "pi"}:
            # Keep source aligned with this parser when callers omit/mismatch it.
            payload = session if session.get("source") == self.id else {**session, "source": self.id}
            return load_session_conversation(payload)

        if self.id == "opencode" or len(inspect.signature(self._load).parameters) >= 2:
            return self._load(str(session.get("path") or ""), str(session.get("id") or ""))
        return self._load(str(session.get("path") or ""))

    def scan_signature(self) -> object | None:
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
    ) -> dict[str, list[SessionInfo]]:
        runtimes = list(self)

        def _one(runtime: RuntimeParser) -> tuple[str, list[SessionInfo], str | None]:
            try:
                return (
                    runtime.id,
                    runtime.scan_sessions(limit, include_missing_cwd=include_missing_cwd),
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
    return ParserRegistry(
        [
            RuntimeParser(
                id="claude",
                display_name="Claude Code",
                _scan=claude.scan_sessions,
                _load=claude.load_conversation,
                _signature=claude.scan_signature,
            ),
            RuntimeParser(
                id="codex",
                display_name="Codex CLI",
                _scan=codex.scan_sessions,
                _load=codex.load_conversation,
                _signature=codex.scan_signature,
            ),
            RuntimeParser(
                id="opencode",
                display_name="OpenCode",
                _scan=opencode.scan_sessions,
                _load=opencode.load_conversation,
                _signature=opencode.scan_signature,
            ),
            RuntimeParser(
                id="kimi",
                display_name="Kimi Code",
                _scan=kimi.scan_sessions,
                _load=kimi.load_conversation,
                _signature=kimi.scan_signature,
            ),
            RuntimeParser(
                id="cursor",
                display_name="Cursor Agent",
                _scan=cursor.scan_sessions,
                _load=cursor.load_conversation,
                _signature=cursor.scan_signature,
            ),
            RuntimeParser(
                id="pi",
                display_name="Pi",
                _scan=pi.scan_sessions,
                _load=pi.load_conversation,
                _signature=pi.scan_signature,
            ),
        ]
    )
