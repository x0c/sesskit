"""Safe output paths for export/share — never clobber live history files."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable


def realpath_or_abs(path: str) -> str:
    """Resolve symlinks when possible; fall back to abspath if the target is gone."""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.abspath(path)


def assert_not_history_path(output_path: str, protected: Iterable[str]) -> None:
    """Refuse writes that would overwrite a session history file (or its symlink)."""
    out = realpath_or_abs(output_path)
    for raw in protected:
        if not raw:
            continue
        protected_path = realpath_or_abs(str(raw))
        if out == protected_path:
            raise ValueError(
                f"refusing to overwrite session history path: {protected_path}"
            )


def atomic_write_json(path: str, payload: object, *, compact: bool = False) -> str:
    """Write JSON via a same-directory temp file, then replace."""
    output_path = os.path.abspath(path)
    parent = os.path.dirname(output_path) or "."
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"output directory does not exist: {parent}")
    if os.path.isdir(output_path):
        raise IsADirectoryError(f"output path is a directory: {output_path}")

    fd, tmp_path = tempfile.mkstemp(prefix=".sesskit-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(
                payload,
                fp,
                ensure_ascii=False,
                separators=(",", ":") if compact else None,
                indent=None if compact else 2,
            )
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path
