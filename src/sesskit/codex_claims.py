"""Read-only Codex hosted-session claim files (Corral-compatible layout)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CLAIM_DIR = Path.home() / ".cache" / "corral" / "codex-claims"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def live_claims(sessions_dir: str) -> dict[str, int]:
    root = Path(sessions_dir).resolve()
    out: dict[str, int] = {}
    try:
        files = list(CLAIM_DIR.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            thread_id = str(data["thread_id"])
            rollout = Path(str(data["rollout_path"])).resolve()
            pid = int(data["pid"])
            suffix = thread_id + ".jsonl"
            if (
                not _UUID.fullmatch(thread_id)
                or not rollout.is_relative_to(root)
                or not rollout.name.endswith(suffix)
            ):
                continue
            os.kill(pid, 0)
            out[thread_id] = pid
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return out
