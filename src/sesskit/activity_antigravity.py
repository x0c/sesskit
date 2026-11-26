"""Typed activity snapshot and byte-offset incremental reader for Antigravity.

Reuses one stateful row-sequence interpreter (``_AntigravityFeed``) and tail
outcome accumulator (``_AntigravityOutcomeAcc``) across full snapshots,
plain-conversation projection, v1 compatibility projection, and incremental
polling over ``transcript_full.jsonl`` / ``transcript.jsonl``.

``step_index`` ordering note: live Antigravity transcripts can append concurrent
tool-result steps slightly out of numeric ``step_index`` order (for example an
immediate ``INVALID`` tool result for the second call landing one line before
the first call finishes). Both snapshots and the byte-offset reader interpret
rows in committed JSONL file order while pairing ``tool_result`` rows to their
``PLANNER_RESPONSE`` calls by expected step offset (``planner_step + 1 + idx``)
with FIFO fallback, so committed event sequence numbers never shift on append.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from typing import Any

from sesskit.activity_reader_jsonl import (
    _AUX_CALL_CAP,
    _dedup_tail_hash,
    _JsonlActivityReaderBase,
)
from sesskit.errors import classify_error, native_http_status
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    AgentError,
    AnswerRecord,
    CompactionInfo,
    Evidence,
    InteractionRequest,
    QuestionItem,
    QuestionOption,
    SessionOutcome,
    SkillInvocation,
    ToolResultOutcome,
    Usage,
)
from sesskit.parsers import antigravity as scan_antigravity
from sesskit.parsers.antigravity_errors import ErrorStep, ErrorStepMerge, read_error_steps
from sesskit.parsers.common import classify_tool, parse_timestamp
from sesskit.skills import raw_skill_name, skill_from_tool_call

_NATIVE = "native"
_INFERRED = "inferred"
_UNKNOWN = "unknown"


def _record_ref(lineno: int) -> str:
    return f"line:{lineno}"


def _nonneg_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _coerce_input(raw: object) -> object:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"raw": raw}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    return {"value": raw}


def _antigravity_usage(row: dict, *, model: str | None, record: str) -> Usage | None:
    in_tok = _nonneg_int(row.get("input_tokens"))
    cache_tok = _nonneg_int(row.get("cache_read_tokens"))
    out_tok = _nonneg_int(row.get("output_tokens"))
    clean_model = model.strip() if isinstance(model, str) and model.strip() else None
    if in_tok is None and cache_tok is None and out_tok is None and clean_model is None:
        return None
    return Usage(
        Evidence(_NATIVE, field="usage", record=record),
        model=clean_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_tok,
    )


def _antigravity_error(
    message: str,
    *,
    record: str,
    field: str = "error",
    scope: str = "turn",
    http_status: int | None = None,
) -> AgentError:
    err_kind, retryable = classify_error(
        runtime="antigravity",
        kind="error",
        code=http_status,
        message=message,
    )
    return AgentError(
        err_kind,
        message,
        Evidence(_NATIVE, field=field, record=record),
        retryable=retryable,
        scope=scope,  # type: ignore[arg-type]
        http_status=http_status or native_http_status(None, message),
    )


def _antigravity_question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    if not isinstance(raw_input, dict):
        return None
    raw_questions = raw_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    items: list[QuestionItem] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            return None
        prompt_raw = item.get("question")
        if not isinstance(prompt_raw, str) or not prompt_raw.strip():
            prompt_raw = item.get("prompt")
        if not isinstance(prompt_raw, str) or not prompt_raw.strip():
            return None
        raw_options = item.get("options")
        if raw_options is not None and not isinstance(raw_options, list):
            return None
        options: list[QuestionOption] = []
        for option in raw_options or ():
            if isinstance(option, str):
                label = option.strip()
                if label:
                    options.append(QuestionOption(label))
            elif isinstance(option, dict):
                label = str(option.get("label") or option.get("text") or "").strip()
                if label:
                    desc = option.get("description")
                    options.append(
                        QuestionOption(
                            label,
                            str(desc).strip() if isinstance(desc, str) and desc.strip() else None,
                        )
                    )
        header = item.get("header") or item.get("title")
        items.append(
            QuestionItem(
                prompt=prompt_raw.strip(),
                title=header.strip() if isinstance(header, str) and header.strip() else None,
                options=tuple(options),
                multi_select=bool(item.get("is_multi_select") or item.get("multiSelect")),
                free_text=True,
            )
        )
    return tuple(items) if items else None


def _attach_antigravity_interactions(
    events: list[ActivityEvent],
    calls: dict[str, tuple[int, str, object, str]],
    results: dict[str, tuple[object, ToolResultOutcome, str]],
) -> None:
    """Attach structured ``InteractionRequest`` records to ``ask_question`` calls."""
    for call_id, (index, name, raw_input, call_record) in calls.items():
        if classify_tool(name) != "question":
            continue
        items = _antigravity_question_items(raw_input)
        if items is None:
            continue
        outcome_entry = results.get(call_id)
        answers: tuple[AnswerRecord, ...] = ()
        if outcome_entry is not None and outcome_entry[1].status == "ok":
            raw_output, result_outcome, _result_record = outcome_entry
            resolution: str = "answered"
            resolution_evidence = result_outcome.evidence
            if len(items) == 1:
                if isinstance(raw_output, dict) and isinstance(raw_output.get("text"), str):
                    answer_text = raw_output["text"].strip()
                elif isinstance(raw_output, str):
                    answer_text = raw_output.strip()
                else:
                    answer_text = ""
                if answer_text:
                    answers = (AnswerRecord(0, answer_text),)
        else:
            resolution = "unknown"
            resolution_evidence = Evidence(_UNKNOWN)
        event = events[index]
        events[index] = replace(
            event,
            interaction=InteractionRequest(
                "question",
                Evidence(_NATIVE, field="tool_calls", record=call_record),
                resolution=resolution,  # type: ignore[arg-type]
                resolution_evidence=resolution_evidence,
                tool_call_id=call_id or None,
                questions=items,
                answers=answers,
            ),
        )


class _AntigravityFeed:
    """Stateful row-sequence interpreter shared by snapshots and the reader."""

    def __init__(self, seq_start: int = 0) -> None:
        self._seq_start = seq_start
        self.events: list[ActivityEvent] = []
        self.current_model: str | None = None
        # call_id -> (event index, tool name, raw input, record)
        self.calls: dict[str, tuple[int, str, object, str]] = {}
        # call_id -> (output payload/text, typed outcome, record)
        self.results: dict[str, tuple[object, ToolResultOutcome, str]] = {}
        self.pending_by_step: dict[int, str] = {}
        self.pending_fifo: list[str] = []

    def _add(
        self,
        event_type: str,
        ts: float | None,
        record: str,
        **fields: Any,
    ) -> int | None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return None
        self.events.append(
            ActivityEvent(
                seq=self._seq_start + len(self.events) + 1,
                type=event_type,  # type: ignore[arg-type]
                evidence=Evidence(_NATIVE, record=record),
                ts=ts,
                **fields,
            )
        )
        return len(self.events) - 1

    def feed(self, rows: list[tuple[int, dict]]) -> list[ActivityEvent]:
        """Interpret one batch of ``(lineno, row)`` pairs in file order."""
        before = len(self.events)
        for lineno, row in rows:
            if not scan_antigravity._is_antigravity_record(row):
                continue
            record = _record_ref(lineno) if lineno else row["_db_error_record"]
            kind = str(row.get("type") or "")
            source = str(row.get("source") or "")
            status_upper = str(row.get("status") or "").upper()
            created = row.get("created_at")
            ts = parse_timestamp(created) if isinstance(created, str) else None
            media = scan_antigravity.normalize_media(row.get("media"))

            if kind == "USER_INPUT":
                self.pending_by_step.clear()
                self.pending_fifo.clear()
                raw_content = row.get("content") or ""
                picked = scan_antigravity.model_name_from_user_input(raw_content)
                if picked:
                    self.current_model = picked
                text = scan_antigravity.unwrap_user_request(raw_content)
                origin = (
                    "human"
                    if source == "USER_EXPLICIT"
                    else ("system" if source == "SYSTEM" else "unknown")
                )
                user_skills: tuple[SkillInvocation, ...] = ()
                if source == "USER_EXPLICIT":
                    explicit_skills = scan_antigravity.extract_explicit_skills_from_user_input(raw_content)
                    if explicit_skills:
                        user_skills = tuple(
                            SkillInvocation(
                                name=raw_skill_name(sname),
                                trigger="explicit",
                                detection="command",
                                evidence=Evidence(_NATIVE, field="ADDITIONAL_METADATA.SKILL", record=record),
                                arguments=sargs,
                            )
                            for sname, sargs in explicit_skills
                            if raw_skill_name(sname)
                        )
                if text and text != "None":
                    extra: dict[str, Any] = {"text": text, "origin": origin}
                    if media:
                        extra["raw_input"] = {"media": media}
                    if user_skills:
                        extra["skills"] = user_skills
                    self._add("user_message", ts, record, **extra)
                elif media:
                    self._add(
                        "user_message",
                        ts,
                        record,
                        text=scan_antigravity.format_media_text(media),
                        origin=origin,
                        raw_input={"media": media},
                    )
                elif status_upper in scan_antigravity._ABORT_STATUSES:
                    err_text = scan_antigravity._row_error_text(row) or (
                        "canceled" if "CANCEL" in status_upper else "error"
                    )
                    self._add(
                        "assistant_message",
                        ts,
                        record,
                        text=err_text,
                        error=_antigravity_error(err_text, record=record, scope="turn"),
                    )
                continue

            if kind == "SYSTEM_MESSAGE":
                if status_upper in scan_antigravity._ABORT_STATUSES:
                    err_text = scan_antigravity._row_error_text(row) or (
                        "canceled" if "CANCEL" in status_upper else "error"
                    )
                    self._add(
                        "assistant_message",
                        ts,
                        record,
                        text=err_text,
                        error=_antigravity_error(err_text, record=record, scope="turn"),
                    )
                continue

            if kind == "CHECKPOINT":
                content = row.get("content")
                summary = content.strip() if isinstance(content, str) and content.strip() else None
                self._add(
                    "compaction",
                    ts,
                    record,
                    text=summary,
                    usage=_antigravity_usage(row, model=self.current_model, record=record),
                    compaction=CompactionInfo(
                        Evidence(_NATIVE, field="type", record=record),
                        summary=summary,
                    ),
                )
                continue

            if kind == "PLANNER_RESPONSE":
                usage = _antigravity_usage(row, model=self.current_model, record=record)
                thinking = row.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    self._add("thinking", ts, record, text=thinking.strip(), usage=usage)
                content = row.get("content")
                text = ""
                if isinstance(content, str) and content.strip():
                    candidate = content.strip()
                    if candidate != "None":
                        text = candidate
                is_abort = status_upper in scan_antigravity._ABORT_STATUSES
                err_text = scan_antigravity._row_error_text(row) if is_abort else ""
                if is_abort and not text:
                    err_msg = err_text or (
                        "canceled" if "CANCEL" in status_upper else "error"
                    )
                    self._add(
                        "assistant_message",
                        ts,
                        record,
                        text=err_msg,
                        error=_antigravity_error(err_msg, record=record, scope="turn"),
                        usage=usage,
                    )
                elif text:
                    extra_asst: dict[str, Any] = {"text": text, "usage": usage}
                    if err_text:
                        extra_asst["error"] = _antigravity_error(err_text, record=record, scope="turn")
                    if media:
                        extra_asst["raw_output"] = {"media": media}
                    self._add("assistant_message", ts, record, **extra_asst)
                elif media and not row.get("tool_calls"):
                    self._add(
                        "assistant_message",
                        ts,
                        record,
                        text=scan_antigravity.format_media_text(media),
                        usage=usage,
                        raw_output={"media": media},
                    )
                calls = row.get("tool_calls")
                if isinstance(calls, list):
                    step_val = row.get("step_index")
                    for index, call in enumerate(calls):
                        if not isinstance(call, dict):
                            continue
                        name = str(call.get("name") or "tool")
                        raw_input = _coerce_input(call.get("args"))
                        step_label = step_val if step_val is not None else index
                        call_id = f"{step_label}-{index}"
                        if call_id in self.calls:
                            call_id = f"{call_id}:{lineno}"
                        ev_index = self._add(
                            "tool_call",
                            ts,
                            record,
                            name=name,
                            call_id=call_id,
                            raw_input=raw_input,
                            skills=skill_from_tool_call(
                                name, raw_input, record=record, call_id=call_id,
                            ),
                        )
                        if ev_index is not None:
                            self.calls[call_id] = (ev_index, name, raw_input, record)
                            self.pending_fifo.append(call_id)
                            if isinstance(step_val, int) and not isinstance(step_val, bool):
                                self.pending_by_step[step_val + 1 + index] = call_id
                continue

            if scan_antigravity.is_tool_result_row(row):
                call_id = scan_antigravity.match_pending_call(
                    self.pending_by_step, self.pending_fifo, row.get("step_index")
                )
                if status_upper == "DONE":
                    outcome = ToolResultOutcome("ok", Evidence(_NATIVE, field="status", record=record))
                elif status_upper in {"ERROR", "INVALID"}:
                    outcome = ToolResultOutcome("error", Evidence(_NATIVE, field="status", record=record))
                else:
                    outcome = ToolResultOutcome("unknown", Evidence(_UNKNOWN))
                content_str = row.get("content").strip() if isinstance(row.get("content"), str) and row.get("content").strip() else ""
                err_str = row.get("error").strip() if isinstance(row.get("error"), str) and row.get("error").strip() else ""
                output_text = content_str or err_str or (
                    status_upper.lower() if status_upper in {"ERROR", "INVALID"} else ""
                )
                if media:
                    raw_output: object = (
                        {"text": output_text, "media": media} if output_text else {"media": media}
                    )
                else:
                    raw_output = output_text
                if output_text or media or call_id:
                    self._add(
                        "tool_result",
                        ts,
                        record,
                        call_id=call_id,
                        raw_output=raw_output,
                        result=outcome,
                    )
                    if call_id:
                        self.results[call_id] = (output_text or raw_output, outcome, record)
                continue

            if kind == "ERROR_MESSAGE" or status_upper in scan_antigravity._ABORT_STATUSES:
                err_msg = scan_antigravity._row_error_text(row) or (
                    "canceled" if "CANCEL" in status_upper else "error"
                )
                self._add(
                    "assistant_message",
                    ts,
                    record,
                    text=err_msg,
                    error=_antigravity_error(
                        err_msg,
                        record=record,
                        field="step_payload" if not lineno else (
                            "error" if kind == "ERROR_MESSAGE" else "status"
                        ),
                        scope="turn",
                        http_status=row.get("_db_http_status") if not lineno else None,
                    ),
                )

        self._link_interactions()
        return self.events[before:]

    def _link_interactions(self) -> None:
        _attach_antigravity_interactions(self.events, self.calls, self.results)

    def finish_snapshot(self) -> list[ActivityEvent]:
        """Run the final interaction pass at end of history."""
        self._link_interactions()
        return []


class _AntigravityOutcomeAcc:
    """Stateful tail-outcome accumulator shared by snapshots and incremental reads."""

    def __init__(self) -> None:
        self._tracker = scan_antigravity.AntigravityTurnTracker()
        self._db_status: int | None = None

    def add(self, lineno: int, row: dict) -> None:
        before = self._tracker.tail_record
        self._tracker.observe(row, lineno=lineno)
        if before != self._tracker.tail_record:
            self._db_status = row.get("_db_http_status") if not lineno else None

    def result(self) -> SessionOutcome:
        tracker = self._tracker
        if not tracker.tail_type:
            return SessionOutcome("unknown", Evidence(_UNKNOWN))
        if tracker.tail_type == "ERROR_MESSAGE" or (
            not tracker.tail_is_tool
            and tracker.tail_status in scan_antigravity._ABORT_STATUSES
        ):
            fallback = "canceled" if "CANCEL" in tracker.tail_status else "error"
            err_text = tracker.tail_error or fallback
            field = (
                "step_payload" if tracker.tail_record.startswith("steps:")
                else "error" if tracker.tail_type == "ERROR_MESSAGE" else "status"
            )
            ev = Evidence(_NATIVE, field=field, record=tracker.tail_record)
            return SessionOutcome(
                "aborted",
                ev,
                error=_antigravity_error(
                    err_text,
                    record=tracker.tail_record,
                    field=field,
                    scope="turn",
                    http_status=self._db_status,
                ),
            )
        if tracker.tail_status == "RUNNING":
            return SessionOutcome(
                "pending",
                Evidence(_NATIVE, field="status", record=tracker.tail_record),
            )
        if (
            tracker.pending_async
            or tracker.tail_is_tool
            or tracker.tail_type == "SYSTEM_MESSAGE"
        ):
            return SessionOutcome(
                "pending",
                Evidence(_INFERRED, field="tail_role", record=tracker.tail_record),
            )
        if tracker.open_tool_calls > 0 or tracker.tail_has_tool_calls:
            return SessionOutcome("unknown", Evidence(_UNKNOWN))
        if (
            tracker.tail_type == "PLANNER_RESPONSE"
            and tracker.tail_status == "DONE"
            and bool(tracker.tail_reply.strip())
        ):
            return SessionOutcome(
                "done",
                Evidence(_INFERRED, field="tail_role", record=tracker.tail_record),
            )
        if tracker.tail_type == "USER_INPUT" and tracker.tail_has_user_text:
            return SessionOutcome(
                "pending",
                Evidence(_INFERRED, field="tail_role", record=tracker.tail_record),
            )
        return SessionOutcome("unknown", Evidence(_UNKNOWN))


def _readable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return True


def load_antigravity_activity(session: dict) -> ActivitySnapshot:
    """Return a typed ``ActivitySnapshot`` for one Antigravity session."""
    path = str(session.get("path") or "")
    if not _readable(path):
        return ActivitySnapshot(
            state="unavailable",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    errors = read_error_steps(path) or ()
    if path.endswith(".db"):
        return _db_activity(errors)
    rows: list[tuple[int, dict]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if scan_antigravity._is_antigravity_record(entry):
                    rows.append((lineno, entry))
    except OSError:
        return ActivitySnapshot(
            state="unavailable",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    return _rows_activity(list(ErrorStepMerge(errors).rows(rows, final=True)))


def _rows_activity(rows: list[tuple[int, dict]]) -> ActivitySnapshot:
    feed = _AntigravityFeed()
    feed.feed(rows)
    feed.finish_snapshot()
    if not feed.events:
        return ActivitySnapshot(
            state="empty",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    acc = _AntigravityOutcomeAcc()
    for lineno, row in rows:
        acc.add(lineno, row)
    return ActivitySnapshot(
        state="available",
        events=tuple(feed.events),
        outcome=acc.result(),
        cursor=None,
        generation=None,
    )


def _db_activity(errors: tuple[ErrorStep, ...]) -> ActivitySnapshot:
    snapshot = _rows_activity([(0, step.row()) for step in errors])
    if errors and not errors[-1].is_db_tail:
        return replace(snapshot, outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)))
    return snapshot


class AntigravityActivityReader(_JsonlActivityReaderBase):
    """Byte-offset JSONL reader with exact-session SQLite error evidence."""

    RUNTIME = "antigravity"
    _error_steps: tuple[ErrorStep, ...] = ()
    _error_hash = ""

    def _prepare_rebuild(self) -> None:
        self._error_merge = ErrorStepMerge(self._error_steps)

    def _prepare_rows(self, rows: list[tuple[int, dict]], *, final: bool = False
                      ) -> list[tuple[int, dict]]:
        if not self._error_steps:
            return rows
        valid = ((line, row) for line, row in rows if scan_antigravity._is_antigravity_record(row))
        return list(self._error_merge.rows(valid, final=final))

    def _new_feed(self) -> _AntigravityFeed:
        return _AntigravityFeed()

    def _new_acc(self) -> _AntigravityOutcomeAcc:
        return _AntigravityOutcomeAcc()

    def _aux_state(self) -> dict[str, Any]:
        feed = self._feed
        last = self._events[-1] if self._events else None
        unresolved: list[str] = []
        if feed is not None:
            unresolved = sorted(
                call_id for call_id in feed.calls if call_id and call_id not in feed.results
            )
        return {
            "model": feed.current_model if feed is not None else None,
            "db_errors": self._error_hash,
            "last_emit": _dedup_tail_hash(last),
            "unresolved": unresolved[:_AUX_CALL_CAP],
            "unresolved_truncated": len(unresolved) > _AUX_CALL_CAP,
        }

    def _try_cheap_resume(self, stat: os.stat_result, saved: dict[str, Any]) -> bool:
        if self._saved_error_hash(saved) != self._error_hash:
            return False
        return super()._try_cheap_resume(stat, saved)

    @staticmethod
    def _saved_error_hash(saved: dict[str, Any]) -> str:
        aux = saved.get("aux")
        return str(aux.get("db_errors") or "") if isinstance(aux, dict) else ""

    def _sync(self) -> None:
        db_only = self._path.endswith(".db")
        steps = read_error_steps(self._path)
        changed = False
        if steps is not None and steps != self._error_steps:
            self._error_steps = steps
            content = tuple((s.index, s.message, s.created_at, s.http_status,
                             db_only and s.is_db_tail) for s in steps)
            error_hash = hashlib.sha256(repr(content).encode()).hexdigest() if content else ""
            changed = error_hash != self._error_hash
            self._error_hash = error_hash
        if not db_only and not (changed and self._initialized):
            super()._sync()
            return
        try:
            stat = os.stat(self._path)
        except OSError:
            self._mark_unavailable()
            return
        if db_only:
            self._sync_db_only(stat, changed)
        else:
            # DB-only writes can insert into the committed JSONL history.
            # Rebuild only when the decoded error evidence actually changes.
            self._rebuild(stat, fresh_gen=self._gen + 1)

    def _sync_db_only(self, stat: os.stat_result, changed: bool) -> None:
        identity = (stat.st_dev, stat.st_ino)
        self._new_events = []
        self._last_reset = False
        if not self._initialized:
            saved, self._pending_cursor = self._pending_cursor, None
            reuse = (saved is not None and (saved.get("dev"), saved.get("ino")) == identity
                     and self._saved_error_hash(saved) == self._error_hash)
            self._gen = (self._resume_gen(saved) + (not reuse) if saved is not None
                         else (2 if self._pending_invalid else 1))
            self._last_reset = not reuse
        elif changed or self._state == "unavailable" or identity != (self._dev, self._ino):
            self._gen += 1
            self._last_reset = True
        else:
            return
        snapshot = _db_activity(self._error_steps)
        self._events = list(snapshot.events)
        if self._last_reset:
            self._new_events = self._events[-self._tail_events:]
        self._state = snapshot.state
        self._outcome = snapshot.outcome
        self._initialized = self._materialized = True
        self._dev, self._ino = identity
        self._size = stat.st_size
        self._mtime_ns = stat.st_mtime_ns

    def _materialize_full(self) -> None:
        if self._path.endswith(".db"):
            self._sync()
            return
        super()._materialize_full()
