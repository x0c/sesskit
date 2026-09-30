"""Shared conformance suite for runtime adapters plus real-history verification.

Stage A of the abstraction redesign: every runtime adapter/loader is run
against the same invariants, first on synthetic fixtures
(``tests/test_conformance.py``) and then on local real sessions through
``sesskit verify``.

Checked invariants:

- ``seq_order``: event ``seq`` starts at 1 and strictly increases.
- ``tool_pairing``: every ``tool_result`` with a call id pairs with an
  earlier ``tool_call``; unpaired results are flagged, never silently ok.
- ``evidence_present``: every typed event carries evidence.
- ``unknown_stays_unknown``: a ``done`` outcome never follows unmatched
  tool calls in the terminal turn (scoped by native ``turn_id`` when
  present; whole history otherwise); known outcomes carry non-unknown
  evidence.
- ``done_has_no_error`` / ``aborted_has_error``: outcome/error consistency.
- ``outcome_tail``: an inferred ``done`` outcome never ends on a user tail.
- ``v1_parity``: the typed snapshot projects byte-equal to ``load_events``.
- ``conversation_*``: plain conversation roles/text stay consistent with
  activity user/assistant text where applicable.
- ``reader_snapshot_match`` / ``reader_page_match`` / ``reader_append_match``:
  a cold-open window equals the matching snapshot suffix (same seqs),
  backward pages reassembled equal the full snapshot, and an append poll
  equals the snapshot suffix with the same outcome.

Violation details carry seq numbers and rule names only, never event text,
ids, or paths, so ``verify`` output is safe to paste.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.activity_reader import open_activity_reader, supports_incremental
from sesskit.models import ActivitySnapshot
from sesskit.registry import ConversationLoadError, ParserRegistry, load_session_conversation
from sesskit.transcript import count_events, load_events
from sesskit.visibility import visible_in_conversation

RUNTIME_IDS = ("claude", "codex", "opencode", "kimi", "cursor", "pi")
TYPED_RUNTIMES = ("pi", "claude", "codex", "cursor", "opencode")
LEGACY_V1_ONLY = ("kimi",)


@dataclass(frozen=True)
class Violation:
    """One failed invariant. ``detail`` holds counts/seqs only, never content."""

    rule: str
    detail: str


@dataclass
class ConformanceReport:
    """Result of running the suite against one session."""

    runtime: str
    state: str
    event_count: int
    violations: tuple[Violation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for violation in self.violations:
            counts[violation.rule] = counts.get(violation.rule, 0) + 1
        return counts


def _iter_typed(events: Sequence[Any]) -> Any:
    for event in events:
        if hasattr(event, "seq"):
            yield event


def _event_seq(event: Any, index: int) -> int | None:
    seq = event.seq if hasattr(event, "seq") else event.get("seq")
    return seq if isinstance(seq, int) else None


def _event_type(event: Any) -> str:
    if hasattr(event, "type"):
        return str(event.type)
    return str(event.get("type") or "")


def _event_text(event: Any) -> str | None:
    text = event.text if hasattr(event, "text") else event.get("text")
    return text if isinstance(text, str) else None


def _event_call_id(event: Any) -> str:
    call_id = event.call_id if hasattr(event, "call_id") else event.get("call_id")
    if call_id is None and hasattr(event, "get"):
        call_id = event.get("id")
    if hasattr(event, "id") and not hasattr(event, "call_id"):
        call_id = event.id
    return str(call_id or "")


def check_seq(events: Sequence[Any]) -> list[Violation]:
    """Event seq starts at 1 and strictly increases by 1."""
    violations: list[Violation] = []
    for index, event in enumerate(events):
        seq = _event_seq(event, index)
        expected = index + 1
        if seq != expected:
            violations.append(Violation(
                "seq_order",
                f"position {index} has seq={seq!r}, expected {expected}",
            ))
    return violations


def check_pairing(events: Sequence[Any]) -> list[Violation]:
    """Every tool_result call id pairs with an earlier tool_call."""
    violations: list[Violation] = []
    seen_calls: set[str] = set()
    for index, event in enumerate(events):
        kind = _event_type(event)
        call_id = _event_call_id(event)
        if kind == "tool_call" and call_id:
            seen_calls.add(call_id)
        elif kind == "tool_result" and call_id and call_id not in seen_calls:
            seq = _event_seq(event, index)
            violations.append(Violation(
                "tool_pairing",
                f"tool_result seq={seq} has no earlier tool_call",
            ))
    return violations


def check_evidence(events: Sequence[Any]) -> list[Violation]:
    """Every typed event carries evidence with a known origin."""
    violations: list[Violation] = []
    for index, event in enumerate(events):
        if not hasattr(event, "seq"):
            continue  # v1 dicts carry no evidence; parity covers them.
        evidence = getattr(event, "evidence", None)
        origin = getattr(evidence, "origin", None)
        if origin not in {"native", "inferred", "unknown"}:
            violations.append(Violation(
                "evidence_present",
                f"event seq={_event_seq(event, index)} lacks evidence",
            ))
    return violations


def check_outcome(snapshot: ActivitySnapshot) -> list[Violation]:
    """Unknown stays unknown; outcome and error stay consistent."""
    violations: list[Violation] = []
    if snapshot.state != "available":
        return violations
    outcome = snapshot.outcome
    events = list(snapshot.events)
    if outcome.status != "unknown" and outcome.evidence.origin == "unknown":
        violations.append(Violation(
            "unknown_stays_unknown",
            f"outcome {outcome.status} carries unknown evidence",
        ))
    if outcome.status == "done" and outcome.error is not None:
        violations.append(Violation("done_has_no_error", "done outcome carries an error"))
    if outcome.status == "aborted" and outcome.error is None:
        violations.append(Violation("aborted_has_error", "aborted outcome carries no error"))
    if outcome.status == "done" and events:
        # Scope the open-call check to the terminal turn: a native
        # ``task_complete`` closes its turn, so an unmatched call from an
        # earlier turn must not block a later clean completion. Producers
        # stamp native turn ids (Codex); without them the whole history
        # stays in scope. Unstamped tool events stay in scope as well.
        terminal_turn = getattr(events[-1], "turn_id", None)
        scoped = [
            e for e in _iter_typed(events)
            if terminal_turn is None
            or getattr(e, "turn_id", None) == terminal_turn
            or (getattr(e, "turn_id", None) is None
                and e.type in {"tool_call", "tool_result"})
        ]
        issued = {e.call_id for e in scoped
                  if e.type == "tool_call" and e.call_id}
        answered = {e.call_id for e in scoped
                    if e.type == "tool_result" and e.call_id}
        if issued - answered:
            violations.append(Violation(
                "unknown_stays_unknown",
                "done outcome with unmatched tool calls",
            ))
        if events[-1].type == "user_message" and outcome.evidence.origin != "native":
            # An inferred done on a user tail upgrades unknown to success;
            # a native terminal marker (e.g. an empty completion record)
            # is completion evidence even when it emits no event.
            violations.append(Violation("outcome_tail", "done outcome ends on a user tail"))
    return violations


def check_v1_parity(snapshot: ActivitySnapshot, v1_events: Sequence[dict]) -> list[Violation]:
    """The typed snapshot must project byte-equal to ``load_events``."""
    if snapshot.state not in {"available", "empty"}:
        return []
    projected = to_v1_dicts(snapshot) if snapshot.state == "available" else []
    if list(projected) == list(v1_events):
        return []
    detail = f"projected {len(projected)} events, loader returned {len(v1_events)}"
    for index, (left, right) in enumerate(zip(projected, v1_events)):
        if left != right:
            keys = sorted(set(left) | set(right))
            differing = sorted(k for k in keys if left.get(k) != right.get(k))
            detail = f"first diff at seq={index + 1}: differing_fields={len(differing)}"
            break
    return [Violation("v1_parity", detail)]


def _conversation_roles(conversation: Sequence[Any]) -> list[Violation]:
    violations: list[Violation] = []
    for index, message in enumerate(conversation):
        role = message.role if hasattr(message, "role") else message.get("role")
        text = message.text if hasattr(message, "text") else message.get("text")
        if role not in {"user", "assistant"}:
            violations.append(Violation("conversation_roles", f"turn {index} has unsupported role"))
        if not isinstance(text, str) or not text.strip() or text.strip() == "None":
            violations.append(Violation("conversation_roles", f"turn {index} has empty text"))
    return violations


def _message_role(message: Any) -> Any:
    return message.role if hasattr(message, "role") else message.get("role")


def _message_text(message: Any) -> Any:
    return message.text if hasattr(message, "text") else message.get("text")


def _visible_conversation_event(event: Any) -> bool:
    if hasattr(event, "seq"):
        return visible_in_conversation(event)
    # v1 has already applied the shared event visibility projection. Only
    # chat-message rows can appear in plain conversation.
    return _event_type(event) in {"user_message", "assistant_message"}


def _expected_conversation_pairs(events: Sequence[Any]) -> list[tuple[str, str]]:
    if any(hasattr(event, "seq") for event in events):
        return [
            (_event_type(event).removesuffix("_message"), text)
            for event in events
            if _event_type(event) in {"user_message", "assistant_message"}
            and _visible_conversation_event(event)
            and isinstance((text := _event_text(event)), str)
            and text.strip()
        ]

    # Kimi's legacy loader joins adjacent assistant text chunks across
    # non-chat rows and flushes at each user message.
    expected: list[tuple[str, str]] = []
    pending_assistant: list[str] = []

    def flush_assistant() -> None:
        if pending_assistant:
            expected.append(("assistant", "\n\n".join(pending_assistant)))
            pending_assistant.clear()

    for event in events:
        kind = _event_type(event)
        text = _event_text(event)
        if kind == "user_message":
            flush_assistant()
            if isinstance(text, str) and text.strip():
                expected.append(("user", text))
        elif kind == "assistant_message" and isinstance(text, str) and text.strip():
            pending_assistant.append(text.strip())
    flush_assistant()
    return expected


def check_conversation(conversation: Sequence[Any], events: Sequence[Any]) -> list[Violation]:
    """Plain conversation exactly matches its ordered, visible event projection."""
    violations = _conversation_roles(conversation)
    actual = [(_message_role(message), _message_text(message)) for message in conversation
              if _message_role(message) in {"user", "assistant"}
              and isinstance(_message_text(message), str)
              and _message_text(message).strip()]
    expected = _expected_conversation_pairs(events)
    for role in ("user", "assistant"):
        actual_role = [pair for pair in actual if pair[0] == role]
        expected_role = [pair for pair in expected if pair[0] == role]
        if actual_role != expected_role:
            violations.append(Violation(
                f"conversation_{role}_coverage",
                f"ordered visible {role} projection differs "
                f"(conversation={len(actual_role)}, events={len(expected_role)})",
            ))
    if actual != expected and all(
            [pair for pair in actual if pair[0] == role]
            == [pair for pair in expected if pair[0] == role]
            for role in ("user", "assistant")):
        violations.append(Violation(
            "conversation_order",
            f"conversation turn order differs (conversation={len(actual)}, events={len(expected)})",
        ))
    return violations


def run_conformance(
    runtime: str,
    *,
    snapshot: ActivitySnapshot | None,
    v1_events: Sequence[dict],
    conversation: Sequence[Any] | None = None,
    compare_views: bool = True,
) -> ConformanceReport:
    """Run the suite against one session's loaded views.

    ``snapshot`` may be ``None`` for runtimes without typed support (Kimi):
    v1-level checks still run against ``v1_events``.
    """
    violations: list[Violation] = []
    if runtime != "kimi" and (snapshot is None or snapshot.state == "unsupported"):
        violations.append(Violation("typed_activity_unavailable",
                                    "runtime declares typed activity but snapshot is unsupported"))
    if snapshot is not None and snapshot.state == "available":
        typed = list(snapshot.events)
        violations.extend(check_seq(typed))
        violations.extend(check_pairing(typed))
        violations.extend(check_evidence(typed))
        violations.extend(check_outcome(snapshot))
        if compare_views:
            violations.extend(check_v1_parity(snapshot, v1_events))
        state = snapshot.state
        count = len(typed)
    else:
        violations.extend(check_seq(list(v1_events)))
        violations.extend(check_pairing(list(v1_events)))
        if snapshot is not None and snapshot.state == "empty" and compare_views:
            violations.extend(check_v1_parity(snapshot, v1_events))
        state = snapshot.state if snapshot is not None else "legacy"
        count = len(v1_events)
    if conversation is not None and compare_views:
        basis = list(snapshot.events) if snapshot is not None and snapshot.state == "available" else list(v1_events)
        violations.extend(check_conversation(conversation, basis))
    return ConformanceReport(runtime=runtime, state=state, event_count=count,
                             violations=tuple(violations))


# --- real-history verification ------------------------------------------------


def _median_ms(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _p95_ms(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return ordered[index]


def _error_kind(exc: Exception) -> str:
    return type(exc).__name__


def _source_stamp(session: dict) -> tuple | None:
    """Best-effort stable identity/version evidence for independent reads."""
    path = str(session.get("path") or "")
    if not path:
        return None
    if str(session.get("source") or "") == "cursor" and os.path.isdir(path):
        paths = [os.path.join(path, "store.db"), os.path.join(path, "store.db-wal"),
                 os.path.join(path, "prompt_history.json")]
    else:
        paths = [path, path + "-wal"]
        if str(session.get("source") or "") == "cursor":
            paths.append(os.path.join(os.path.dirname(path), "prompt_history.json"))
    stamps: list[tuple] = []
    for candidate in paths:
        try:
            stat = os.stat(candidate)
        except FileNotFoundError:
            stamps.append(("missing",))
        except OSError as exc:
            stamps.append(("unreadable", type(exc).__name__))
        else:
            stamps.append((stat.st_dev, stat.st_ino, stat.st_size,
                           stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(stamps)


def _source_exists(session: dict) -> bool:
    """Confirm a scanned session still has one of its native source files."""
    path = str(session.get("path") or "")
    if not path:
        return False
    if str(session.get("source") or "") == "cursor":
        if os.path.isdir(path):
            return any(os.path.isfile(os.path.join(path, name))
                       for name in ("store.db", "prompt_history.json"))
        return (os.path.isfile(path)
                or os.path.isfile(os.path.join(os.path.dirname(path), "prompt_history.json")))
    return bool(os.path.isfile(path))


def _is_exact_suffix(window: Sequence[Any], full: Sequence[Any]) -> bool:
    if len(window) > len(full):
        return False
    if not window:
        return not full
    return list(window) == list(full[-len(window):])


def verify_session(session: dict) -> dict:
    """Load one real session through every view and run conformance.

    Returns a JSON-safe dict with counts and timings only, never content
    or paths.
    """
    runtime = str(session.get("source") or "unknown")
    source_present = _source_exists(session)
    started = time.perf_counter()
    source_stamps: list[tuple | None] = [_source_stamp(session)]
    try:
        conversation = load_session_conversation(session)
        load_ok = True
        load_error: str | None = None
    except ConversationLoadError as exc:
        conversation = []
        load_ok = False
        load_error = _error_kind(exc)
    except Exception as exc:  # noqa: BLE001 — counted, never raised
        conversation = []
        load_ok = False
        load_error = _error_kind(exc)
    conversation_ms = (time.perf_counter() - started) * 1000
    source_stamps.append(_source_stamp(session))

    started = time.perf_counter()
    try:
        snapshot = load_activity(session)
        activity_load_error = None
    except Exception as exc:  # noqa: BLE001 — recorded as a failed load
        from sesskit.models import Evidence, SessionOutcome
        snapshot = ActivitySnapshot("unavailable", (), SessionOutcome("unknown", Evidence("unknown")))
        activity_load_error = _error_kind(exc)
    snapshot_ms = (time.perf_counter() - started) * 1000
    source_stamps.append(_source_stamp(session))
    if snapshot.state == "unavailable" and activity_load_error is None:
        activity_load_error = "Unavailable"
    activity_load_ok = activity_load_error is None or (
        snapshot.state == "unsupported" and runtime == "kimi")

    started = time.perf_counter()
    try:
        v1_events = load_events(session)
        event_load_error = None
    except Exception as exc:  # noqa: BLE001 — counted, never raised
        v1_events = []
        event_load_error = _error_kind(exc)
    v1_ms = (time.perf_counter() - started) * 1000
    source_stamps.append(_source_stamp(session))

    source_stable = (source_stamps[0] is not None
                     and all(stamp == source_stamps[0] for stamp in source_stamps[1:]))
    views_available = (source_present and load_ok and activity_load_ok and event_load_error is None
                       and snapshot.state in {"available", "empty", "unsupported"})

    report = run_conformance(runtime, snapshot=snapshot, v1_events=v1_events,
                             conversation=conversation if load_ok else None,
                             compare_views=bool(source_stable and views_available))
    violations = [{"rule": v.rule, "detail": v.detail} for v in report.violations]
    if not load_ok:
        violations.append({"rule": "conversation_load", "detail": "conversation load failed"})
    if not activity_load_ok:
        violations.append({"rule": "activity_load", "detail": "activity load failed or was unavailable"})
    if event_load_error is not None:
        violations.append({"rule": "event_load", "detail": "v1 event load failed"})
    if not source_present:
        violations.append({"rule": "source_missing", "detail": "scanned session source is absent"})
    reader_info: dict[str, Any] = {"supported": False, "ok": True,
                                   "reset": None, "mismatch": False,
                                   "page_ok": None,
                                   "open_poll_ms": None, "error_kind": None,
                                   "comparison_state": "not_applicable"}
    try:
        reader_supported = supports_incremental(session)
    except Exception as exc:  # noqa: BLE001 — capability probe itself can fail
        reader_supported = False
        reader_info.update(ok=False, error_kind=_error_kind(exc),
                           comparison_state="failed")
    if reader_supported:
        reader_source_before = _source_stamp(session)
        source_stamps.append(reader_source_before)
        reader_info["supported"] = True
        reader_info["comparison_state"] = "inconclusive"
        started = time.perf_counter()
        try:
            reader = open_activity_reader(session)
            poll = reader.poll()
            reader_info["open_poll_ms"] = (time.perf_counter() - started) * 1000
            reader_info["reset"] = bool(poll.reset)
            if source_stable and views_available:
                expected_state = snapshot.state
                state_matches = poll.state == expected_state
                events_match = (snapshot.state == "available"
                                and (list(poll.events) == list(snapshot.events)
                                     or _is_exact_suffix(poll.events, snapshot.events)))
                if snapshot.state == "empty":
                    events_match = not poll.events
                outcome_matches = poll.outcome == snapshot.outcome
                reader_info["mismatch"] = not (
                    state_matches and poll.reset and events_match and outcome_matches)
                reader_info["comparison_state"] = "verified"
            # Backward pages reassembled must equal the full snapshot with
            # dense seqs and intact call/result order, in every generation;
            # a generation change mid-paging (live writer) is inconclusive,
            # never a failure.
            try:
                assembled: list = []
                before_token = None
                pg = None
                generation_changed = False
                for _ in range(200):
                    pg = reader.page(before=before_token, limit=200)
                    if str(pg.generation) != str(poll.generation):
                        generation_changed = True
                        break
                    assembled = list(pg.events) + assembled
                    if not pg.has_more:
                        break
                    before_token = pg.before
                else:
                    reader_info["page_ok"] = False
                if generation_changed:
                    reader_info["page_ok"] = None
                    reader_info["mismatch"] = False
                    reader_info["comparison_state"] = "inconclusive"
                elif reader_info["page_ok"] is None and pg is not None and str(
                        pg.generation) == str(poll.generation):
                    if source_stable and views_available and snapshot.state in {"available", "empty"}:
                        reader_info["page_ok"] = (
                            assembled == list(snapshot.events)
                            and poll.outcome == snapshot.outcome
                            and poll.state == snapshot.state
                        )
                        if reader_info["page_ok"]:
                            reader_info["comparison_state"] = "verified"
            except Exception as exc:  # noqa: BLE001 — counted, never raised
                reader_info["page_ok"] = False
                reader_info["ok"] = False
                reader_info["error_kind"] = _error_kind(exc)
        except Exception as exc:  # noqa: BLE001 — counted, never raised
            reader_info["ok"] = False
            reader_info["error_kind"] = _error_kind(exc)
            reader_info["open_poll_ms"] = (time.perf_counter() - started) * 1000
            reader_info["comparison_state"] = "failed"
        source_stamps.append(_source_stamp(session))
    elif runtime == "kimi":
        reader_info["comparison_state"] = "unsupported"
    elif runtime in TYPED_RUNTIMES:
        reader_info.update(ok=False, error_kind="UnsupportedCapability",
                           comparison_state="failed")
        violations.append({"rule": "reader_unavailable",
                           "detail": "runtime declares incremental reading but has no reader"})

    source_stable = (source_stamps[0] is not None
                     and all(stamp == source_stamps[0] for stamp in source_stamps[1:]))
    if not source_stable and reader_info["supported"] and reader_info["ok"]:
        # Independent reader and snapshot reads may straddle an append or
        # checkpoint. Preserve any operation error, but do not call the
        # resulting comparison a pass or a content mismatch.
        reader_info["mismatch"] = False
        if reader_info["page_ok"] is not False:
            reader_info["page_ok"] = None
        reader_info["comparison_state"] = "inconclusive"

    if reader_info["mismatch"] and source_stable:
        violations.append({"rule": "reader_snapshot_match",
                           "detail": "incremental poll state, events, or outcome differ from snapshot"})
    if reader_info["page_ok"] is False and source_stable:
        violations.append({"rule": "reader_page_match",
                           "detail": "backward pages do not equal the full snapshot"})
    if not reader_info["ok"]:
        violations.append({"rule": "reader_failure", "detail": "reader operation failed"})

    failed = bool(violations)
    comparison_state = "verified" if source_stable else "inconclusive"
    verification_state = ("failed" if failed else
                         "inconclusive" if not source_stable
                         or (reader_info["supported"] and reader_info["page_ok"] is None)
                         else "passed")

    return {
        "runtime": runtime,
        "verification_state": verification_state,
        "comparison_state": comparison_state,
        "source_stable": source_stable,
        "source_present": source_present,
        "load_ok": load_ok,
        "load_error_kind": load_error,
        "activity_load_ok": activity_load_ok,
        "activity_load_error_kind": activity_load_error,
        "event_load_ok": event_load_error is None,
        "event_load_error_kind": event_load_error,
        "state": snapshot.state,
        "outcome": snapshot.outcome.status,
        "event_count": report.event_count,
        "event_types": count_events(v1_events),
        "evidence": _evidence_counts(snapshot),
        "result_status": _result_counts(snapshot),
        "parity_ok": (None if not (source_stable and views_available
                                   and snapshot.state in {"available", "empty"}) else
                      event_load_error is None and
                      not any(v.get("rule") == "v1_parity" for v in violations)),
        "violations": violations,
        "conversation_ms": round(conversation_ms, 2),
        "snapshot_ms": round(snapshot_ms, 2),
        "v1_ms": round(v1_ms, 2),
        "reader": reader_info,
    }


def _evidence_counts(snapshot: ActivitySnapshot) -> dict[str, int]:
    counts = {"native": 0, "inferred": 0, "unknown": 0}
    if snapshot.state != "available":
        return counts
    for event in snapshot.events:
        origin = getattr(getattr(event, "evidence", None), "origin", "unknown")
        counts[origin] = counts.get(origin, 0) + 1
    return counts


def _result_counts(snapshot: ActivitySnapshot) -> dict[str, int]:
    counts = {"ok": 0, "error": 0, "unknown": 0}
    if snapshot.state != "available":
        return counts
    for event in snapshot.events:
        result = getattr(event, "result", None)
        if result is not None:
            counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def _probe_append_rows(runtime_id: str) -> list[dict] | None:
    """Two plain text rows for the append probe (no speculative edges)."""
    if runtime_id == "claude":
        return [
            {"type": "user", "timestamp": "2026-09-30T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "reader probe question"}]}},
            {"type": "assistant", "timestamp": "2026-09-30T00:00:02Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "reader probe answer"}]}},
        ]
    if runtime_id == "codex":
        return [
            {"type": "response_item", "timestamp": 1_700_000_001.0,
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "reader probe question"}]}},
            {"type": "response_item", "timestamp": 1_700_000_002.0,
             "payload": {"type": "message", "role": "assistant", "content": [
                 {"type": "output_text", "text": "reader probe answer"}]}},
        ]
    return None


def probe_jsonl_reader(session: dict) -> dict[str, Any]:
    """Append-parity probe for JSONL readers on a temp copy (never mutates history).

    Copies the history file aside, cold-opens a reader, appends two rows,
    and checks the append poll equals the snapshot suffix with the same
    outcome. Also records cold tail open, no-change poll, and backward
    page timings on the largest history. Counts and timings only.
    """
    runtime_id = str(session.get("source") or "")
    outcome: dict[str, Any] = {"checked": False, "append_parity_ok": None,
                               "nochange_empty_ok": None, "cold_ms": None,
                               "nochange_ms": None, "page_ms": None,
                               "append_ms": None, "events_before": None,
                               "size_bytes": None, "error_kind": None,
                               "inconclusive": False, "skip_reason": None}
    rows = _probe_append_rows(runtime_id)
    src = str(session.get("path") or "")
    if rows is None or not src or not os.path.isfile(src):
        outcome["skip_reason"] = "source_unavailable"
        return outcome
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "history.jsonl")
            source_before = _source_stamp(session)
            shutil.copyfile(src, dst)
            if source_before is None or source_before != _source_stamp(session):
                outcome["inconclusive"] = True
                outcome["skip_reason"] = "source_changed_during_copy"
                return outcome
            probe_session = dict(session, path=dst)
            outcome["size_bytes"] = os.path.getsize(dst)
            started = time.perf_counter()
            reader = open_activity_reader(probe_session)
            baseline = reader.poll()
            outcome["cold_ms"] = round((time.perf_counter() - started) * 1000, 2)
            outcome["events_before"] = len(getattr(reader, "_events", ()))
            started = time.perf_counter()
            again = reader.poll()
            outcome["nochange_ms"] = round((time.perf_counter() - started) * 1000, 2)
            outcome["nochange_empty_ok"] = (
                again.events == () and not again.reset
                and again.state == baseline.state
                and again.outcome == baseline.outcome
            )
            started = time.perf_counter()
            reader.page(limit=50)
            outcome["page_ms"] = round((time.perf_counter() - started) * 1000, 2)
            with open(dst, "a", encoding="utf-8") as handle:
                handle.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            started = time.perf_counter()
            delta = reader.poll()
            outcome["append_ms"] = round((time.perf_counter() - started) * 1000, 2)
            after = load_activity(probe_session)
            before_total = outcome["events_before"] or 0
            outcome["append_parity_ok"] = bool(
                not delta.reset
                and delta.state == after.state
                and list(delta.events) == list(after.events[before_total:])
                and delta.outcome == after.outcome)
            outcome["checked"] = True
    except Exception as exc:  # noqa: BLE001 — counted, never raised
        outcome["error_kind"] = _error_kind(exc)
    return outcome


def _empty_runtime_report(sample: int) -> dict[str, Any]:
    return {
        "verification_state": "unverified",
        "sample": sample,
        "scanned": 0,
        "sampled": 0,
        "load_ok": 0,
        "load_failed": 0,
        "activity_load_failed": 0,
        "event_load_failed": 0,
        "load_errors": {},
        "states": {},
        "parity_ok": 0,
        "parity_failed": 0,
        "parity_not_applicable": 0,
        "parity_inconclusive": 0,
        "violations": {},
        "event_types": {name: 0 for name in ("user_message", "assistant_message",
                                             "thinking", "tool_call", "tool_result")},
        "evidence": {"native": 0, "inferred": 0, "unknown": 0},
        "outcomes": {},
        "result_status": {"ok": 0, "error": 0, "unknown": 0},
        "reader": {"supported": 0, "ok": 0, "failed": 0, "mismatched": 0, "page_failed": 0},
        "timings_ms": {"scan": None, "snapshot_median": None, "snapshot_p95": None,
                       "reader_median": None, "reader_p95": None},
    }


def verify_runtime(registry: ParserRegistry, runtime_id: str, sample: int) -> dict[str, Any]:
    """Verify up to ``sample`` recent real sessions for one runtime."""
    report = _empty_runtime_report(sample)
    if sample < 0:
        raise ValueError("sample limit must be non-negative")
    if sample == 0:
        report["skip_reason"] = "sample_limit_zero"
        return report
    started = time.perf_counter()
    try:
        sessions = registry.get(runtime_id).scan_sessions(sample)
        scan_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — counted, never raised
        sessions = []
        scan_error = _error_kind(exc)
    report["timings_ms"]["scan"] = round((time.perf_counter() - started) * 1000, 2)
    if scan_error is not None:
        report["scan_error_kind"] = scan_error
        report["verification_state"] = "failed"
        report["violations"]["scan_failure"] = 1
        return report
    report["scanned"] = len(sessions)
    if not sessions:
        report["skip_reason"] = "no_sessions"
        return report
    snapshot_times: list[float] = []
    reader_times: list[float] = []
    session_results: list[dict] = []
    largest: dict | None = None
    largest_bytes = -1
    for session in sessions[: max(0, sample)]:
        result = verify_session(dict(session))
        session_results.append(result)
        report["sampled"] += 1
        try:
            size_bytes = int(dict(session).get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes >= largest_bytes:
            largest_bytes = size_bytes
            largest = dict(session)
        if result["load_ok"]:
            report["load_ok"] += 1
        else:
            report["load_failed"] += 1
            kind = result["load_error_kind"] or "Unknown"
            report["load_errors"][kind] = report["load_errors"].get(kind, 0) + 1
        if not result["activity_load_ok"]:
            report["activity_load_failed"] += 1
        if not result["event_load_ok"]:
            report["event_load_failed"] += 1
        report["states"][result["state"]] = report["states"].get(result["state"], 0) + 1
        if result["state"] in {"unavailable", "unsupported", "legacy"}:
            report["parity_not_applicable"] += 1
        elif result["comparison_state"] != "verified":
            report["parity_inconclusive"] += 1
        elif result["parity_ok"]:
            report["parity_ok"] += 1
        else:
            report["parity_failed"] += 1
        for violation in result["violations"]:
            rule = violation["rule"]
            report["violations"][rule] = report["violations"].get(rule, 0) + 1
        for name, count in result["event_types"].items():
            report["event_types"][name] = report["event_types"].get(name, 0) + count
        for origin, count in result["evidence"].items():
            report["evidence"][origin] = report["evidence"].get(origin, 0) + count
        outcome = result["outcome"]
        report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
        for status, count in result["result_status"].items():
            report["result_status"][status] = report["result_status"].get(status, 0) + count
        snapshot_times.append(result["snapshot_ms"])
        reader = result["reader"]
        if reader["supported"]:
            report["reader"]["supported"] += 1
            if reader["ok"] and not reader["mismatch"]:
                report["reader"]["ok"] += 1
            else:
                report["reader"]["failed"] += 1
            if reader["mismatch"]:
                report["reader"]["mismatched"] += 1
            if reader["page_ok"] is False:
                report["reader"]["page_failed"] += 1
            if reader["open_poll_ms"] is not None:
                reader_times.append(reader["open_poll_ms"])
    report["timings_ms"]["snapshot_median"] = _median_ms(snapshot_times)
    report["timings_ms"]["snapshot_p95"] = _p95_ms(snapshot_times)
    report["timings_ms"]["reader_median"] = _median_ms(reader_times)
    report["timings_ms"]["reader_p95"] = _p95_ms(reader_times)
    if runtime_id in {"claude", "codex"} and largest is not None:
        probe = probe_jsonl_reader(largest)
        if probe["checked"]:
            probe["verification_state"] = (
                "passed" if probe["append_parity_ok"] is True
                and probe["nochange_empty_ok"] is True else "failed")
        elif probe["error_kind"]:
            probe["verification_state"] = "failed"
        elif probe["inconclusive"]:
            probe["verification_state"] = "inconclusive"
        else:
            probe["verification_state"] = "unverified"
        report["reader_probe"] = probe
        if probe["verification_state"] == "failed":
            report["violations"]["reader_append_match"] = (
                report["violations"].get("reader_append_match", 0) + 1)
    states = [result["verification_state"] for result in session_results]
    probe_state = report.get("reader_probe", {}).get("verification_state")
    if not report["sampled"]:
        report["verification_state"] = "unverified"
    elif any(state == "failed" for state in states) or report["violations"]:
        report["verification_state"] = "failed"
    elif any(state == "inconclusive" for state in states) or probe_state == "inconclusive":
        report["verification_state"] = "inconclusive"
    elif probe_state == "unverified":
        report["verification_state"] = "unverified"
    elif runtime_id in {"claude", "codex"} and largest is not None and (
            report.get("reader_probe", {}).get("verification_state") != "passed"):
        report["verification_state"] = "failed"
    else:
        report["verification_state"] = "passed"
    return report


def verify_all(registry: ParserRegistry, runtime_ids: Sequence[str], sample: int) -> dict[str, Any]:
    """Verify real sessions for the given runtimes; counts and timings only."""
    runtimes = {rid: verify_runtime(registry, rid, sample) for rid in runtime_ids}
    totals = _empty_runtime_report(sample)
    totals["runtimes"] = sorted(runtimes)
    for single in runtimes.values():
        totals["scanned"] += single["scanned"]
        totals["sampled"] += single["sampled"]
        totals["load_ok"] += single["load_ok"]
        totals["load_failed"] += single["load_failed"]
        totals["activity_load_failed"] += single["activity_load_failed"]
        totals["event_load_failed"] += single["event_load_failed"]
        totals["parity_ok"] += single["parity_ok"]
        totals["parity_failed"] += single["parity_failed"]
        totals["parity_not_applicable"] += single["parity_not_applicable"]
        totals["parity_inconclusive"] += single["parity_inconclusive"]
        for key in ("load_errors", "states", "violations", "event_types",
                    "evidence", "outcomes", "result_status"):
            for name, count in single[key].items():
                totals[key][name] = totals[key].get(name, 0) + count
        for key in ("supported", "ok", "failed", "mismatched", "page_failed"):
            totals["reader"][key] += single["reader"][key]
    all_verified = bool(runtimes) and all(
        single["verification_state"] == "passed" for single in runtimes.values())
    passed = (all_verified and totals["parity_failed"] == 0
              and not totals["violations"]
              and totals["reader"]["mismatched"] == 0
              and totals["reader"]["failed"] == 0)
    state = "passed" if passed else (
        "failed" if any(single["verification_state"] == "failed"
                         for single in runtimes.values())
        else "inconclusive" if any(single["verification_state"] == "inconclusive"
                                   for single in runtimes.values())
        else "unverified")
    totals["verification_state"] = state
    return {"sample": sample, "runtimes": runtimes, "totals": totals,
            "verification_state": state, "passed": passed}
