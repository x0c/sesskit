"""Snapshot CLI handlers for offline version and explicit installation lifecycle."""

from __future__ import annotations

from sesskit import __version__
from sesskit.envelope import EXIT_ERROR, ApiError, ok
from sesskit.lifecycle import UpgradeError, upgrade


def cmd_version(_args, _registry) -> dict:
    return ok({"name": "sesskit", "version": __version__})


def cmd_upgrade(args, _registry) -> dict:
    try:
        result = upgrade(dry_run=bool(getattr(args, "dry_run", False)))
    except UpgradeError as exc:
        raise ApiError(
            exc.code,
            exc.message,
            EXIT_ERROR,
            hint=exc.hint,
            next_commands=exc.next_commands,
        ) from exc
    return ok(result)
