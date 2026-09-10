"""Read-only Pi session identity claims (Corral extension layout)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
IDENTITY_DIRNAME = "corral-session-identity"
INSTANCE_ENV = "CORRAL_PI_INSTANCE_ID"
CLAIM_PROTOCOL = 1
CLAIM_TTL_SECONDS = 60.0


def pi_agent_dir(root: str | os.PathLike[str] | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    override = os.environ.get(PI_AGENT_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pi" / "agent"


def claims_dir(root: str | os.PathLike[str] | None = None) -> Path:
    return pi_agent_dir(root) / IDENTITY_DIRNAME / "claims" / "v1"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_claims(root: str | os.PathLike[str] | None = None) -> list[dict]:
    directory = claims_dir(root)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    claims: list[dict] = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        claim = _read_json(directory / name)
        if claim is not None:
            claims.append(claim)
    return claims


def claim_is_live(claim: dict | None, now: datetime | None = None) -> bool:
    if not isinstance(claim, dict):
        return False
    if claim.get("protocolVersion") != CLAIM_PROTOCOL:
        return False
    if claim.get("state") not in ("active", "switching"):
        return False
    session_id = str(claim.get("sessionId") or "").strip()
    if not session_id:
        return False
    try:
        pid = int(claim.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    updated = _parse_iso(claim.get("updatedAt"))
    if updated is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return now - updated <= timedelta(seconds=CLAIM_TTL_SECONDS)


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
