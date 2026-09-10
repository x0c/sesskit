"""Hosted-session env helpers understood by Corral-compatible agent hosts."""

from __future__ import annotations

import os
from pathlib import Path

PROCESS_ENV_KEYS: tuple[str, ...] = (
    "CORRAL_SESSION_ID",
    "PICKUP_SESSION_ID",
    "SC_SESSION_ID",
    "SESSKIT_SESSION_ID",
    "CORRAL_RUNTIME",
    "PICKUP_RUNTIME",
    "SC_RUNTIME",
    "SESSKIT_RUNTIME",
    "PI_CODING_AGENT_SESSION_DIR",
    "CORRAL_PI_INSTANCE_ID",
    "CORRAL_PI_CLAIM_PATH",
)

HOSTED_DIR_PREFIX = "corral-"
LEGACY_HOSTED_DIR_PREFIXES: tuple[str, ...] = ("pickup-",)


def hosted_session_id(env: dict[str, str] | None) -> str:
    if not env:
        return ""
    return (
        env.get("CORRAL_SESSION_ID")
        or env.get("PICKUP_SESSION_ID")
        or env.get("SC_SESSION_ID")
        or env.get("SESSKIT_SESSION_ID")
        or ""
    )


def hosted_isolation_dirname(ident: str) -> str:
    return f"{HOSTED_DIR_PREFIX}{ident}"


def is_hosted_isolation_dir(directory: str) -> bool:
    base = os.path.basename(str(directory or "").rstrip("/"))
    return base.startswith((HOSTED_DIR_PREFIX, *LEGACY_HOSTED_DIR_PREFIXES))


def cache_dir() -> Path:
    """Default ``~/.cache/sesskit``; honor ``SESSKIT_CACHE_DIR`` then ``CORRAL_CACHE_DIR``."""
    override = os.environ.get("SESSKIT_CACHE_DIR") or os.environ.get("CORRAL_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return root / "sesskit"
