"""Runtime parser registry — scan/list without launch or handoff."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from sesskit.models import ConversationMessage, SessionInfo
from sesskit.parsers import claude, codex, cursor, kimi, opencode, pi

ScanFn = Callable[..., list[SessionInfo]]
LoadFn = Callable[..., list[ConversationMessage]]
SigFn = Callable[[], object | None]


@dataclass
class RuntimeParser:
    id: str
    display_name: str
    _scan: ScanFn
    _load: LoadFn
    _signature: SigFn | None = None

    def scan_sessions(self, limit: int = 50, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        # Parser modules use keyword ``limit`` (positional arg 0 is cwd_filter).
        import inspect

        params = inspect.signature(self._scan).parameters
        kwargs: dict = {"limit": limit}
        if keep_ids is not None and "keep_ids" in params:
            kwargs["keep_ids"] = keep_ids
        return self._scan(**kwargs)

    def load_conversation(self, session: dict) -> list[ConversationMessage]:
        return self._load(session)

    def scan_signature(self) -> object | None:
        if self._signature is None:
            return None
        return self._signature()


class ParserRegistry:
    def __init__(self, runtimes: list[RuntimeParser]):
        self._runtimes = {r.id: r for r in runtimes}
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

    def scan_all(self, limit: int) -> dict[str, list[SessionInfo]]:
        runtimes = list(self)

        def _one(runtime: RuntimeParser) -> list[SessionInfo]:
            try:
                return runtime.scan_sessions(limit)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
            scanned = pool.map(_one, runtimes)
        return {runtime.id: result for runtime, result in zip(runtimes, scanned, strict=True)}


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
