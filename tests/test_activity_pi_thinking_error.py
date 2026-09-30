"""W5: Pi thinking-only error turns must surface their error text.

An assistant entry with ``stopReason`` in {error, aborted} plus
``errorMessage`` whose content holds only non-empty ``thinking`` parts
previously marked the entry emitted via the thinking event (stop reason
but no error), so no typed error was produced. The builder now emits one
typed-only ``lifecycle`` error event; the v1 projection skips it, so v1
bytes stay identical. Covers snapshot and incremental reader paths.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.activity_reader import open_activity_reader
from sesskit.transcript import load_events
from sesskit.turns import derive_turns


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(path: Path, session_id: str = "pi-w5") -> dict:
    return {"source": "pi", "path": str(path), "id": session_id}


def _header(session_id: str = "pi-w5") -> dict:
    return {"type": "session", "id": session_id,
            "timestamp": "2026-09-01T00:00:00Z", "cwd": "/tmp/pi-w5-fixture"}


def _entry(entry_id: str | None, parent_id: str | None, role: str,
           content: object, timestamp: str, **message_fields: object) -> dict:
    message = {"role": role, "content": content, **message_fields}
    item: dict = {"type": "message", "timestamp": timestamp, "message": message}
    if entry_id is not None:
        item["id"] = entry_id
    if parent_id is not None:
        item["parentId"] = parent_id
    return item


def _thinking_error_entries() -> list[dict]:
    return [
        _header(),
        _entry("u1", None, "user", [{"type": "text", "text": "Keep going."}],
               "2026-09-01T00:00:01Z"),
        _entry("a-err", "u1", "assistant",
               [{"type": "thinking", "thinking": "Trying the upstream call."}],
               "2026-09-01T00:00:02Z",
               stopReason="error",
               errorMessage="Rate limited: too many requests (429)"),
    ]


def _aborted_thinking_entries() -> list[dict]:
    return [
        _header(),
        _entry("u1", None, "user", [{"type": "text", "text": "Keep going."}],
               "2026-09-01T00:00:01Z"),
        _entry("a-abort", "u1", "assistant",
               [{"type": "thinking", "thinking": "Halfway through the plan."}],
               "2026-09-01T00:00:02Z",
               stopReason="aborted", errorMessage="turn_aborted by user"),
    ]


def _text_error_entries() -> list[dict]:
    return [
        _header(),
        _entry("u1", None, "user", [{"type": "text", "text": "Keep going."}],
               "2026-09-01T00:00:01Z"),
        _entry("a-err", "u1", "assistant",
               [{"type": "text", "text": "Partial reply before the failure."}],
               "2026-09-01T00:00:02Z",
               stopReason="error",
               errorMessage="Provider overloaded (529)"),
    ]


def _lifecycle_errors(snapshot) -> list:
    return [e for e in snapshot.events if e.type == "lifecycle" and e.error is not None]


class PiThinkingOnlyErrorTests(unittest.TestCase):
    def test_thinking_only_error_gains_typed_only_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, _thinking_error_entries())
            session = _session(path)
            snapshot = load_activity(session)

            self.assertEqual(snapshot.state, "available")
            self.assertEqual(
                [e.type for e in snapshot.events],
                ["user_message", "thinking", "lifecycle"],
            )
            error_event = snapshot.events[-1]
            assert error_event.error is not None
            self.assertEqual(error_event.error.kind, "rate_limited")
            self.assertEqual(error_event.error.scope, "turn")
            self.assertEqual(error_event.error.evidence.origin, "native")
            self.assertEqual(error_event.error.evidence.field, "errorMessage")
            self.assertEqual(error_event.stop_reason, "error")
            self.assertEqual(
                error_event.error.message, "Rate limited: too many requests (429)")
            # Tail outcome already aborted; now the typed error exists too.
            self.assertEqual(snapshot.outcome.status, "aborted")
            turns = derive_turns(snapshot)
            self.assertEqual(turns[-1].outcome.status, "failed")
            self.assertEqual(turns[-1].outcome.stop_reason, "error")
            # v1 transcript bytes unchanged.
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_aborted_thinking_turn_gains_interrupted_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, _aborted_thinking_entries())
            session = _session(path)
            snapshot = load_activity(session)

            errors = _lifecycle_errors(snapshot)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].error.kind, "user_interrupt")
            self.assertEqual(errors[0].stop_reason, "aborted")
            self.assertEqual(snapshot.outcome.status, "aborted")
            turns = derive_turns(snapshot)
            self.assertEqual(turns[-1].outcome.status, "interrupted")
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_text_plus_error_turn_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, _text_error_entries())
            session = _session(path)
            snapshot = load_activity(session)

            # The assistant text already carries the error: no extra event.
            self.assertEqual(
                [e.type for e in snapshot.events],
                ["user_message", "assistant_message"],
            )
            assistant = snapshot.events[-1]
            assert assistant.error is not None
            self.assertEqual(assistant.error.kind, "provider_overloaded")
            self.assertEqual(_lifecycle_errors(snapshot), [])
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_reader_matches_snapshot_on_thinking_only_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, _thinking_error_entries())
            session = _session(path)
            snapshot = load_activity(session)
            result = open_activity_reader(session).poll()

            self.assertTrue(result.reset)
            self.assertEqual(result.state, "available")
            self.assertEqual(
                [(e.type, e.text, e.message_id) for e in result.events],
                [(e.type, e.text, e.message_id) for e in snapshot.events],
            )
            self.assertEqual([e.seq for e in result.events],
                             [e.seq for e in snapshot.events])
            self.assertEqual(len(_lifecycle_errors(snapshot)), 1)
            self.assertEqual(result.outcome, snapshot.outcome)

    def test_reader_append_of_thinking_error_stays_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Keep going."}],
                       "2026-09-01T00:00:01Z"),
            ])
            session = _session(path)
            reader = open_activity_reader(session)
            first = reader.poll()
            self.assertEqual([e.type for e in first.events], ["user_message"])
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    _entry("a-err", "u1", "assistant",
                           [{"type": "thinking",
                             "thinking": "Trying the upstream call."}],
                           "2026-09-01T00:00:02Z",
                           stopReason="error",
                           errorMessage="Rate limited: too many requests (429)"),
                    ensure_ascii=False) + "\n")
            delta = reader.poll()
            self.assertFalse(delta.reset)
            self.assertEqual([e.type for e in delta.events], ["thinking", "lifecycle"])
            assert delta.events[-1].error is not None
            self.assertEqual(delta.events[-1].error.kind, "rate_limited")
            snapshot = load_activity(session)
            # Full reader state equals the fresh snapshot event-for-event.
            paged = reader.page(limit=200)
            self.assertEqual(
                [(e.type, e.text, e.message_id) for e in paged.events],
                [(e.type, e.text, e.message_id) for e in snapshot.events],
            )
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))


if __name__ == "__main__":
    unittest.main()
