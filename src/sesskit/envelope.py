"""JSON envelope helpers and exit codes for the SessKit CLI."""

from __future__ import annotations

import json

API_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 5


class ApiError(Exception):
    def __init__(self, code, message, exit_code=EXIT_ERROR, hint=None, next_commands=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.next_commands = next_commands or []


def print_envelope(payload: dict, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def ok(data) -> dict:
    return {"ok": True, "data": data, "error": None, "meta": {"version": API_VERSION}}


def err(exc: ApiError) -> dict:
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "next_commands": exc.next_commands,
        },
        "meta": {"version": API_VERSION},
    }
