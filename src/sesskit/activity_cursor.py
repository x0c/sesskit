"""Typed activity adapter for Cursor's persisted chat-store blobs.

This module mirrors the current Cursor v1 event stream while adding evidence
and typed question details where the native records support them. The
incremental reader below reuses the same row-sequence interpreter
(``_CursorFeed``): warm append polls read only rows after the committed
``rowid`` plus a bounded recheck window, never the whole store window.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import replace
from typing import Any

from sesskit.activity_reader import (
    ActivityReader,
    PageResult,
    PollResult,
    _encode_cursor,
    decode_reader_cursor,
    page_window,
)
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    Evidence,
    InteractionRequest,
    QuestionItem,
    QuestionOption,
    SessionOutcome,
    ToolResultOutcome,
)
from sesskit.parsers import cursor as scan_cursor
from sesskit.parsers.common import classify_tool

_NATIVE = "native"
_INFERRED = "inferred"
_UNKNOWN = "unknown"
_FAILURE_RE = re.compile(
    r"^(?:exit code:?\s*[1-9]|error:|traceback \(most recent call last\)|command failed|fatal:)",
    re.IGNORECASE | re.MULTILINE,
)


def load_cursor_activity(session: dict) -> ActivitySnapshot:
    """Load typed Cursor activity for one scanned session dictionary.

    Cursor's stored rows contain no reliable turn-completion marker, so the
    outcome remains unknown rather than inferring success or pending from the
    last visible message. ``cursor`` and ``generation`` are intentionally None.
    """
    store_path, chat_dir = _paths(session)
    if not store_path:
        return _snapshot("unavailable", [])
    if os.path.isfile(store_path):
        loaded, rows = _read_store(store_path)
    else:
        loaded, rows = False, []
    state, events, _from_prompts, _feed = _resolve_cursor_state(loaded, rows, chat_dir)
    return _snapshot(state, events)


def _resolve_cursor_state(
    loaded: bool,
    rows: list[tuple[int, object]],
    chat_dir: str,
) -> tuple[str, list[ActivityEvent], bool, _CursorFeed | None]:
    """Shared store-plus-prompt resolution for the snapshot and the reader.

    One code path owns the fallback: readable store rows that normalize to
    events win; otherwise ``prompt_history.json`` prompts (oldest-first)
    stand in, never appended to a non-empty store stream. The third element
    reports whether the returned events came from the prompt fallback; the
    fourth carries the feed backing store-mode events (prompt mode has none)
    so the reader keeps interpretation state across polls.
    """
    if loaded:
        feed = _CursorFeed()
        feed.feed(rows)
        feed.finish()
        if feed.events:
            return "available", feed.events, False, feed
        prompts = _prompt_events(chat_dir)
        if prompts:
            return "available", prompts, True, None
        return "empty", [], True, None
    prompts, prompt_state = _read_prompt_events(chat_dir)
    if prompts:
        return "available", prompts, True, None
    if prompt_state == "empty":
        return "empty", [], True, None
    return "unavailable", [], True, None


def _prompt_boundary(events: list[ActivityEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update((event.text or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _snapshot(state: str, events: list[ActivityEvent]) -> ActivitySnapshot:
    return ActivitySnapshot(
        state=state,  # type: ignore[arg-type]
        events=tuple(events),
        outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        cursor=None,
        generation=None,
    )


def _paths(session: dict) -> tuple[str, str]:
    path = str(session.get("path") or "")
    if not path:
        return "", ""
    if path.endswith("store.db"):
        return path, os.path.dirname(path)
    return os.path.join(path, "store.db"), path


def _read_store(store_path: str) -> tuple[bool, list[tuple[int, object]]]:
    conn = scan_cursor.connect_store_ro(store_path)
    if conn is None:
        return False, []
    try:
        rows = conn.execute("SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'7B' ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return False, []
    finally:
        conn.close()
    return True, rows


def _read_prompt_events(chat_dir: str) -> tuple[list[ActivityEvent], str]:
    prompt_path = os.path.join(chat_dir, "prompt_history.json")
    if not os.path.isfile(prompt_path):
        return [], "unavailable"
    try:
        with open(prompt_path, encoding="utf-8") as handle:
            prompts = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], "unavailable"
    if not isinstance(prompts, list):
        return [], "unavailable"
    texts = [str(item).strip() for item in reversed(prompts) if str(item or "").strip()]
    return _user_events(texts, "prompt_history.json"), "empty"


def _prompt_events(chat_dir: str) -> list[ActivityEvent]:
    events, _state = _read_prompt_events(chat_dir)
    return events


def _user_events(texts: list[str], record: str) -> list[ActivityEvent]:
    return [
        ActivityEvent(
            seq=index,
            type="user_message",
            text=text,
            evidence=Evidence(_NATIVE, field="prompt_history", record=record),
            origin="human",
        )
        for index, text in enumerate(texts, 1)
    ]


def _build_events(rows: list[tuple[int, object]]) -> list[ActivityEvent]:
    """Build events over every row; the snapshot path of ``_CursorFeed``."""
    feed = _CursorFeed()
    feed.feed(rows)
    feed.finish()
    return feed.events


class _CursorFeed:
    """Stateful row-sequence interpreter shared by snapshots and the reader.

    ``_build_events`` feeds every row at once; the incremental reader feeds
    one appended batch per poll while this object carries the pending
    tool-result map, emitted call ids, and emission state across polls, so
    both paths run the same interpretation row for row. Linking (question
    details) runs per batch: each call index is visited exactly once.
    ``finish()`` flushes results that arrived without an observed call
    (snapshot-of-prefix semantics); a later batch carrying the missing call
    must take the slow path so the rebuilt order matches the snapshot.
    """

    def __init__(self, seq_start: int = 0) -> None:
        self._seq_start = seq_start
        self.events: list[ActivityEvent] = []
        self.calls: list[tuple[int, str, object, int]] = []
        self.pending: dict[str, tuple[object, ToolResultOutcome, int]] = {}
        self.emitted_calls: set[str] = set()
        self.orphan_results: set[str] = set()
        self.flushed_total = 0

    def _add(self, event_type: str, rowid: int, **fields: Any) -> int | None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return None
        self.events.append(
            ActivityEvent(
                seq=self._seq_start + len(self.events) + 1,
                type=event_type,  # type: ignore[arg-type]
                evidence=Evidence(_NATIVE, record=f"rowid:{rowid}"),
                **fields,
            )
        )
        return len(self.events) - 1

    def _add_result(self, call_id: str, output: object, rowid: int) -> None:
        outcome = _tool_outcome(output, rowid)
        self._add(
            "tool_result",
            rowid,
            call_id=call_id,
            raw_output=output,
            result=outcome,
        )

    def feed(self, rows: list[tuple[int, object]]) -> tuple[list[ActivityEvent], bool]:
        """Interpret one batch of ``(rowid, data)`` rows, appending events.

        Returns the appended events plus whether any tool call references a
        previously flushed orphan result (the caller must take the slow path
        then so the rebuilt order matches the snapshot).
        """
        before = len(self.events)
        hit_orphan = False
        for rowid, data in rows:
            obj = _decode_blob(data)
            if obj is None:
                continue
            role = obj.get("role")
            content = obj.get("content")
            if role == "user":
                text = scan_cursor.user_text_from_blob(obj)
                if text:
                    self._add("user_message", rowid, text=text, origin="human")
                else:
                    raw = scan_cursor._text_from_content(content)
                    if isinstance(raw, str) and raw.strip():
                        # The conversation view drops <user_info>/rules context
                        # blocks; they surface here as typed-only injected events.
                        self._add("user_message", rowid, text=raw.strip(), origin="injected")
                continue
            if role == "assistant":
                if not isinstance(content, list):
                    text = content.strip() if isinstance(content, str) else ""
                    self._add("assistant_message", rowid, text=text)
                    continue
                hit_orphan = self._assistant_parts(content, rowid) or hit_orphan
                continue
            if role == "tool" and isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "tool-result":
                        continue
                    call_id = str(part.get("toolCallId") or "")
                    output = part.get("result")
                    outcome = _tool_outcome(output, rowid)
                    payload = (output, outcome, rowid)
                    if call_id and call_id not in self.emitted_calls:
                        self.pending[call_id] = payload
                    else:
                        self._add_result(call_id, output, rowid)
        fresh = self.events[before:]
        self._link_new_calls()
        return fresh, hit_orphan

    def _assistant_parts(self, content: list[object], rowid: int) -> bool:
        hit_orphan = False
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"thinking", "reasoning"}:
                text = str(part.get("text") or part.get("thinking") or "").strip()
                self._add("thinking", rowid, text=text)
            elif part_type == "text":
                self._add("assistant_message", rowid, text=str(part.get("text") or "").strip())
            elif part_type == "tool-call":
                call_id = str(part.get("toolCallId") or "")
                name = str(part.get("toolName") or "tool")
                raw_input = _json_args(part.get("args"))
                index = self._add(
                    "tool_call",
                    rowid,
                    name=name,
                    call_id=call_id,
                    raw_input=raw_input,
                )
                if index is None:
                    continue
                self.calls.append((index, call_id, raw_input, rowid))
                if call_id:
                    if call_id in self.orphan_results:
                        hit_orphan = True
                    self.emitted_calls.add(call_id)
                    held = self.pending.pop(call_id, None)
                    if held is not None:
                        output, _outcome, result_rowid = held
                        self._add_result(call_id, output, result_rowid)
        return hit_orphan

    def _link_new_calls(self) -> None:
        _attach_interactions(self.events, self.calls)
        self.calls = []

    def finish(self) -> list[ActivityEvent]:
        """Flush results that arrived without an observed call (end of batch).

        Matches the snapshot's end-of-history flush, so the materialized list
        always equals the snapshot over the rows seen so far. Flushed ids are
        recorded: a later call for one of them needs a rebuild (reset), never
        a silent reorder.
        """
        before = len(self.events)
        for call_id, (output, _outcome, rowid) in self.pending.items():
            self._add_result(call_id, output, rowid)
            if call_id and call_id not in self.emitted_calls:
                self.orphan_results.add(call_id)
        self.flushed_total += len(self.events) - before
        self.pending = {}
        return self.events[before:]


def _decode_blob(data: object) -> dict | None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if not raw.startswith(b"{"):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _json_args(raw: object) -> object:
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _tool_outcome(output: object, rowid: int) -> ToolResultOutcome:
    if not isinstance(output, str) or not output.strip():
        return ToolResultOutcome("unknown", Evidence(_UNKNOWN))
    if _FAILURE_RE.search(output[:600]):
        return ToolResultOutcome(
            "error",
            Evidence(_INFERRED, field="content[].result", record=f"rowid:{rowid}"),
        )
    return ToolResultOutcome("unknown", Evidence(_UNKNOWN))


def _question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    if not isinstance(raw_input, dict):
        return None
    raw_questions = raw_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    items: list[QuestionItem] = []
    title = raw_input.get("title")
    for question in raw_questions:
        if not isinstance(question, dict):
            return None
        question_id = question.get("id")
        prompt = question.get("prompt")
        if not isinstance(question_id, (str, int)) or not str(question_id).strip():
            return None
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        options: list[QuestionOption] = []
        raw_options = question.get("options", [])
        if not isinstance(raw_options, list):
            return None
        for option in raw_options:
            if not isinstance(option, dict):
                return None
            option_id = option.get("id")
            label = option.get("label")
            if not isinstance(option_id, (str, int)) or not str(option_id).strip():
                return None
            if not isinstance(label, str) or not label.strip():
                return None
            options.append(QuestionOption(label.strip()))
        multi = question.get("allow_multiple") is True
        items.append(
            QuestionItem(
                prompt=prompt.strip(),
                title=title.strip() if isinstance(title, str) and title.strip() else None,
                options=tuple(options),
                multi_select=multi,
            )
        )
    return tuple(items)


def _attach_interactions(
    events: list[ActivityEvent],
    calls: list[tuple[int, str, object, int]],
) -> None:
    for index, call_id, raw_input, rowid in calls:
        event = events[index]
        if classify_tool(event.name or "") != "question":
            continue
        questions = _question_items(raw_input)
        if questions is None:
            continue
        events[index] = replace(
            event,
            interaction=InteractionRequest(
                purpose="question",
                evidence=Evidence(_NATIVE, field="args.questions", record=f"rowid:{rowid}"),
                resolution="unknown",
                resolution_evidence=Evidence(_UNKNOWN),
                tool_call_id=call_id or None,
                questions=questions,
            ),
        )


# --- Incremental reader -----------------------------------------------------


_CURSOR_ROWS_SQL = (
    "SELECT rowid, data FROM blobs "
    "WHERE substr(data, 1, 1) = X'7B' ORDER BY rowid"
)

# Append-delta queries: warm polls must not re-read the whole store window.
# New rows arrive with greater ``rowid`` (range scan over the rowid index);
# the recheck window below catches in-place content changes.
_CURSOR_MAX_SQL = "SELECT MAX(rowid), COUNT(*) FROM blobs"
_CURSOR_TAIL_SQL = (
    "SELECT rowid, length(data), substr(data, 1, 256) FROM blobs "
    "WHERE rowid <= ? ORDER BY rowid DESC LIMIT ?"
)
_CURSOR_NEW_SQL = "SELECT rowid, data FROM blobs WHERE rowid > ? ORDER BY rowid"
_CURSOR_ONE_SQL = "SELECT data FROM blobs WHERE rowid = ?"

# Bounded recheck window: trailing rows whose (rowid, length, head-bytes)
# signature is compared before accepting an append without a rebuild.
# Same-length edits outside the boundary row can slip past the length check;
# the boundary row itself is always checksum-verified in full.
_CURSOR_RECHECK_ROWS = 8
_CURSOR_HEAD_BYTES = 256


def _file_state(path: str) -> tuple[int, int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _boundary_checksum(rowid: int, data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(str(rowid).encode())
    digest.update(b":")
    digest.update(data)
    return digest.hexdigest()


def _tail_signature(fetched: list[tuple]) -> tuple:
    """Cheap per-row signature over a recheck window: ``(rowid, length,
    sha256(first bytes))`` per row, newest first. Lengths catch content
    replacement; the head hash catches same-length edits near the head of
    each blob without transferring multi-megabyte tool outputs."""
    sig: list[tuple] = []
    for rowid, length, head in fetched:
        try:
            head_bytes = bytes(head or b"")
        except (TypeError, ValueError):
            head_bytes = b""
        try:
            sig.append((
                int(rowid),
                int(length or 0),
                hashlib.sha256(head_bytes).hexdigest(),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(sig)


def _event_key(event: ActivityEvent) -> tuple:
    """Stable identity for prefix comparison across rebuilds (no content)."""
    result = getattr(event, "result", None)
    return (
        event.type,
        event.text,
        event.call_id,
        event.name,
        json.dumps(event.raw_input, sort_keys=True, default=str)
        if event.raw_input is not None else None,
        json.dumps(event.raw_output, sort_keys=True, default=str)
        if event.raw_output is not None else None,
        result.status if result is not None else None,
        event.origin,
    )


class CursorActivityReader(ActivityReader):
    """Incremental reader over one Cursor ``store.db`` chat store.

    The store is read-only with WAL-visible tails (never ``immutable=1``),
    so uncheckpointed appends are seen. The opaque cursor carries a database
    fingerprint (file identity plus WAL state plus SQLite ``data_version``),
    the last committed row position (raw ``max(rowid)``), the boundary-row
    checksum, the event count, and compact interpreter aux state (pending
    results, emitted call ids, flushed orphans). A no-change poll stats the
    main file, its ``-wal`` sidecar, and the prompt file only and never opens
    the database. A warm append poll reads only rows after the committed
    ``rowid`` plus a bounded recheck window (per-row length/head signature
    plus a full checksum of the boundary row) and feeds them through the
    carried ``_CursorFeed``; it never re-reads or rebuilds the whole store
    window. When readable store rows normalize to zero events the reader
    serves the same prompt fallback as the snapshot through the shared
    resolver (never a copy); a prompt append extends the generation while
    store rows appearing later switch from fallback to store with a new
    generation. A vacuum, rowid reuse, in-place edit inside the window,
    checkpoint reshuffle that moves the boundary, file replacement, or
    invalid cursor starts a new generation: the returned events replace, not
    extend, the previous generation.
    """

    def __init__(self, session: dict, cursor: Any = None) -> None:
        store_path, chat_dir = _paths(session)
        self._store_path = store_path
        self._chat_dir = chat_dir
        self._session_id = str(session.get("id") or store_path or "unknown")
        saved = decode_reader_cursor(cursor, "cursor")
        if saved is not None and (
            saved.get("path") != store_path or saved.get("session") != self._session_id
        ):
            saved = None
        self._pending_cursor = saved
        self._gen = 0
        self._events: list[ActivityEvent] = []
        self._feed: _CursorFeed | None = None
        self._previous_events: list[ActivityEvent] = []
        self._tail_sig: tuple = ()
        self._row_count = 0
        self._boundary_rowid = 0
        self._file: tuple[int, int, int, int] | None = None
        self._wal: tuple[int, int, int, int] | None = None
        self._prompt_stat: tuple[int, int, int, int] | None = None
        self._prompt_count = 0
        self._prompt_boundary = hashlib.sha256(b"").hexdigest()
        self._mode = ""
        self._data_version = 0
        self._max_rowid = 0
        self._boundary = hashlib.sha256(b"").hexdigest()
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._state = "unavailable"
        self._initialized = False
        # Test/observability counters: database opens and blob rows
        # JSON-decoded for event building (recheck signature reads excluded).
        self.db_opens = 0
        self.rows_parsed = 0

    # -- public protocol -------------------------------------------------

    def poll(self) -> PollResult:
        self._sync()
        return PollResult(
            events=tuple(self._new_events),
            reset=self._last_reset,
            generation=str(self._gen),
            cursor=self._export_cursor(),
            outcome=self._outcome,
            state=self._state,
        )

    def page(self, before: str | None = None, limit: int = 50) -> PageResult:
        self._sync()
        return page_window(self._events, self._gen, before, limit)

    # -- sync machinery --------------------------------------------------

    def _prompt_path(self) -> str | None:
        if not self._chat_dir:
            return None
        return os.path.join(self._chat_dir, "prompt_history.json")

    def _sync(self) -> None:
        self._new_events: list[ActivityEvent] = []
        self._last_reset = False
        if not self._store_path:
            self._mark_unavailable()
            return
        file_state = _file_state(self._store_path)
        wal_state = _file_state(self._store_path + "-wal")
        prompt_path = self._prompt_path()
        prompt_stat = _file_state(prompt_path) if prompt_path else None
        if not self._initialized:
            self._cold_open(file_state, wal_state, prompt_stat)
            return
        cached_file = self._file or (-1, -1, -1, -1)
        if file_state is not None and (
            file_state[0] != cached_file[0] or file_state[1] != cached_file[1]
        ):
            self._rebuild(file_state, wal_state, prompt_stat, reason="replacement")
            return
        if (
            file_state == self._file
            and wal_state == self._wal
            and (self._mode == "store" or prompt_stat == self._prompt_stat)
        ):
            return
        self._refresh(file_state, wal_state, prompt_stat)

    def _open(self) -> sqlite3.Connection | None:
        conn = scan_cursor.connect_store_ro(self._store_path)
        if conn is not None:
            self.db_opens += 1
        return conn

    @staticmethod
    def _fetch_data_version(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("PRAGMA data_version").fetchone()
        except sqlite3.Error:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def _read_rows(self, conn: sqlite3.Connection) -> list[tuple[int, bytes]] | None:
        try:
            fetched = conn.execute(_CURSOR_ROWS_SQL).fetchall()
        except sqlite3.Error:
            return None
        rows: list[tuple[int, bytes]] = []
        for rowid, data in fetched:
            try:
                rows.append((int(rowid), bytes(data)))
            except (TypeError, ValueError):
                continue
        self.rows_parsed += len(rows)
        return rows

    def _cold_open(
        self,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
    ) -> None:
        loaded, rows, data_version, meta = self._read_store_full(file_state)
        state, events, from_prompts, feed = _resolve_cursor_state(
            loaded, rows, self._chat_dir)
        if state == "unavailable":
            self._mark_unavailable()
            return
        saved = self._pending_cursor
        self._pending_cursor = None
        self._adopt(
            loaded, rows, data_version, meta, file_state, wal_state, prompt_stat,
            events, from_prompts, feed, fresh_gen=1,
        )
        self._initialized = True
        if saved is not None and self._cursor_matches(saved):
            try:
                self._gen = max(int(saved.get("gen", 1)), 1)
            except (TypeError, ValueError):
                self._gen = 1
            self._last_reset = False
            self._new_events = []
        else:
            if saved is not None:
                self._gen += 1
            self._last_reset = True
            self._new_events = list(self._events)

    def _read_store_rows(
        self,
        file_state: tuple[int, int, int, int] | None,
    ) -> tuple[bool, list[tuple[int, bytes]], int]:
        """Read store rows; ``(False, [], 0)`` when missing or unreadable."""
        loaded, rows, data_version, _meta = self._read_store_full(file_state)
        return loaded, rows, data_version

    def _read_store_full(
        self,
        file_state: tuple[int, int, int, int] | None,
    ) -> tuple[bool, list[tuple[int, bytes]], int, tuple | None]:
        """Full store read plus raw position meta, for cold opens and resets.

        Returns ``(loaded, rows, data_version, meta)`` where ``rows`` are the
        JSON-filtered ``(rowid, data)`` pairs and ``meta`` is
        ``(max_rowid, row_count, tail_sig, boundary_rowid, boundary)`` over
        all blobs (``None`` when the meta queries fail).
        """
        if file_state is None:
            return False, [], 0, None
        conn = self._open()
        if conn is None:
            return False, [], 0, None
        try:
            data_version = self._fetch_data_version(conn)
            rows = self._read_rows(conn)
            meta = self._full_meta(conn)
        finally:
            conn.close()
        if rows is None or meta is None:
            return False, [], 0, None
        return True, rows, data_version, meta

    def _full_meta(self, conn: sqlite3.Connection) -> tuple | None:
        """Raw position meta over all blobs; ``None`` on any SQLite error."""
        try:
            mag = conn.execute(_CURSOR_MAX_SQL).fetchone()
            if mag is None or mag[0] is None:
                return (0, 0, (), 0, hashlib.sha256(b"").hexdigest())
            max_rowid, row_count = int(mag[0]), int(mag[1])
            tail = conn.execute(
                _CURSOR_TAIL_SQL, (max_rowid, _CURSOR_RECHECK_ROWS)).fetchall()
        except sqlite3.Error:
            return None
        return (max_rowid, row_count, _tail_signature(tail), 0,
                hashlib.sha256(b"").hexdigest())

    def _refresh(
        self,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
    ) -> None:
        """Stat changed: append-only delta when possible, else a full re-read.

        Store mode attempts the fast path (new ``rowid`` rows plus a bounded
        recheck window, fed through the carried feed); any missing evidence
        or detected change falls back to the slow full re-read below, which
        decides delta versus reset by event-prefix comparison. Prompt mode
        and a missing store file always take the slow path.
        """
        if self._mode == "store" and file_state is not None and self._feed is not None:
            conn = self._open()
            if conn is None:
                # Transient lock or checkpoint race on a store-backed
                # generation: keep the cached generation and report no change
                # rather than flapping.
                return
            try:
                handled = self._try_delta(conn, file_state, wal_state, prompt_stat)
                if handled is True:
                    return
                if handled is None:
                    return
                self._slow_on(conn, file_state, wal_state, prompt_stat)
            finally:
                conn.close()
            return
        loaded, rows, data_version, meta = self._read_store_full(file_state)
        if not loaded and file_state is not None and self._mode == "store" and self._events:
            # Transient lock or checkpoint race on a store-backed
            # generation: keep the cached generation and report no change
            # rather than flapping to the fallback. A missing store file
            # (file_state None) always resolves through the fallback below.
            return
        self._slow_adopt(loaded, rows, data_version, meta,
                         file_state, wal_state, prompt_stat)

    def _try_delta(
        self,
        conn: sqlite3.Connection,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
    ) -> bool | None:
        """Append-only fast path. True when handled (delta or no change),
        False when the slow full re-read must decide, None on transient
        SQLite errors (keep the cached generation)."""
        assert self._feed is not None
        try:
            data_version = self._fetch_data_version(conn)
            mag = conn.execute(_CURSOR_MAX_SQL).fetchone()
            if mag is None or mag[0] is None:
                return False
            new_max, new_count = int(mag[0]), int(mag[1])
            if new_max < self._max_rowid:
                return False
            tail = conn.execute(
                _CURSOR_TAIL_SQL, (self._max_rowid, _CURSOR_RECHECK_ROWS)).fetchall()
            if _tail_signature(tail) != self._tail_sig:
                return False
            if new_max == self._max_rowid:
                if new_count != self._row_count:
                    return False
                if self._boundary_rowid:
                    blob = conn.execute(
                        _CURSOR_ONE_SQL, (self._boundary_rowid,)).fetchone()
                    if blob is None or _boundary_checksum(
                            self._boundary_rowid, bytes(blob[0])) != self._boundary:
                        return False
                self._file = file_state
                self._wal = wal_state
                self._data_version = data_version
                self._prompt_stat = prompt_stat
                self._last_reset = False
                self._new_events = []
                return True
            fetched = conn.execute(_CURSOR_NEW_SQL, (self._max_rowid,)).fetchall()
            if new_count != self._row_count + len(fetched):
                return False
        except sqlite3.Error:
            return None
        raw: list[tuple[int, bytes]] = []
        for rowid, data in fetched:
            try:
                raw.append((int(rowid), bytes(data)))
            except (TypeError, ValueError):
                continue
        self.rows_parsed += len(raw)
        flushed_before = self._feed.flushed_total
        fresh, hit_orphan = self._feed.feed(raw)
        flushed = self._feed.finish()
        if hit_orphan or (flushed_before > 0 and (fresh or flushed)):
            # A call landed for an earlier flushed orphan result, or new
            # events would follow previously flushed orphans: either way the
            # snapshot order may differ, so rebuild and compare.
            return False
        self._previous_events = list(self._events)
        self._events = self._feed.events
        self._max_rowid = new_max
        self._row_count = new_count
        json_rows = [(rowid, data) for rowid, data in raw
                      if data.startswith(b"{")]
        if json_rows:
            last_rowid, last_data = json_rows[-1]
            self._boundary_rowid = last_rowid
            self._boundary = _boundary_checksum(last_rowid, last_data)
        merged: list[tuple] = list(self._tail_sig)
        for rowid, data in raw:
            merged.append((rowid, len(data),
                           hashlib.sha256(data[:_CURSOR_HEAD_BYTES]).hexdigest()))
        merged.sort(key=lambda item: item[0], reverse=True)
        self._tail_sig = tuple(merged[:_CURSOR_RECHECK_ROWS])
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version
        self._prompt_stat = prompt_stat
        self._last_reset = False
        self._new_events = list(fresh) + list(flushed)
        return True

    def _slow_adopt(
        self,
        loaded: bool,
        rows: list[tuple[int, bytes]],
        data_version: int,
        meta: tuple | None,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
    ) -> None:
        state, events, from_prompts, feed = _resolve_cursor_state(
            loaded, rows, self._chat_dir)
        if state == "unavailable":
            self._mark_unavailable()
            return
        self._adopt(
            loaded, rows, data_version, meta, file_state, wal_state, prompt_stat,
            events, from_prompts, feed, fresh_gen=self._gen,
        )
        if self._events_prefix_match():
            self._last_reset = False
            self._new_events = list(self._events[len(self._previous_events):])
        else:
            self._gen += 1
            self._last_reset = True
            self._new_events = list(self._events)

    def _slow_on(
        self,
        conn: sqlite3.Connection,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
    ) -> None:
        """Slow full re-read over an already-open connection, then diff."""
        try:
            data_version = self._fetch_data_version(conn)
            rows = self._read_rows(conn)
            meta = self._full_meta(conn)
        except sqlite3.Error:
            return
        if rows is None or meta is None:
            if file_state is not None and self._mode == "store" and self._events:
                return
            self._slow_adopt(False, [], 0, None, file_state, wal_state, prompt_stat)
            return
        self._slow_adopt(True, rows, data_version, meta,
                         file_state, wal_state, prompt_stat)

    def _rebuild(
        self,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
        reason: str,
    ) -> None:
        loaded, rows, data_version, meta = self._read_store_full(file_state)
        state, events, from_prompts, feed = _resolve_cursor_state(
            loaded, rows, self._chat_dir)
        if state == "unavailable":
            self._mark_unavailable()
            return
        self._previous_events = list(self._events)
        self._adopt(
            loaded, rows, data_version, meta, file_state, wal_state, prompt_stat,
            events, from_prompts, feed, fresh_gen=self._gen + 1,
        )
        self._initialized = True
        self._last_reset = True
        self._new_events = list(self._events)

    def _adopt(
        self,
        loaded: bool,
        rows: list[tuple[int, bytes]],
        data_version: int,
        meta: tuple | None,
        file_state: tuple[int, int, int, int] | None,
        wal_state: tuple[int, int, int, int] | None,
        prompt_stat: tuple[int, int, int, int] | None,
        resolved: list[ActivityEvent],
        from_prompts: bool,
        feed: _CursorFeed | None,
        fresh_gen: int,
    ) -> None:
        self._previous_events = list(self._events)
        self._feed = feed if not from_prompts else None
        self._events = resolved
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._state = "available" if resolved else "empty"
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version if loaded else 0
        self._mode = "prompt" if from_prompts else "store"
        self._prompt_stat = prompt_stat
        if from_prompts and resolved:
            self._prompt_count = len(resolved)
            self._prompt_boundary = _prompt_boundary(resolved)
        else:
            self._prompt_count = 0
            self._prompt_boundary = hashlib.sha256(b"").hexdigest()
        if meta is not None:
            max_rowid, row_count, tail_sig, _brow, _bnd = meta
            self._max_rowid = max_rowid
            self._row_count = row_count
            self._tail_sig = tail_sig
        elif rows:
            last_rowid, _last_data = rows[-1]
            self._max_rowid = last_rowid
            self._row_count = len(rows)
            self._tail_sig = ()
        else:
            self._max_rowid = 0
            self._row_count = 0
            self._tail_sig = ()
        if rows:
            last_rowid, last_data = rows[-1]
            self._boundary_rowid = last_rowid
            self._boundary = _boundary_checksum(last_rowid, last_data)
        else:
            self._boundary_rowid = 0
            self._boundary = hashlib.sha256(b"").hexdigest()
        self._gen = fresh_gen

    def _events_prefix_match(self) -> bool:
        """True when the rebuild only appended: the cached event prefix is
        unchanged, so the poll is a delta. Anything else (vacuum, reuse,
        replacement, reorder, in-place edit) starts a new generation.
        """
        old = list(getattr(self, "_previous_events", []))
        if len(self._events) < len(old):
            return False
        old_keys = [_event_key(event) for event in old]
        new_keys = [_event_key(event) for event in self._events[: len(old)]]
        return old_keys == new_keys

    def _mark_unavailable(self) -> None:
        had_history = self._initialized and self._gen > 0 and self._state != "unavailable"
        self._state = "unavailable"
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._new_events = []
        self._last_reset = had_history

    def _cursor_matches(self, saved: dict[str, Any]) -> bool:
        try:
            if self._file is None:
                if saved.get("dev") is not None or saved.get("ino") is not None:
                    # Old cursors always carried dev/ino; a missing store
                    # file can never match them.
                    return False
            else:
                if int(saved.get("dev", -1)) != self._file[0]:
                    return False
                if int(saved.get("ino", -1)) != self._file[1]:
                    return False
            if int(saved.get("data_version", -1)) != self._data_version:
                return False
            if int(saved.get("max_rowid", -1)) != self._max_rowid:
                return False
            if saved.get("boundary") != self._boundary:
                return False
            if int(saved.get("events", -1)) != len(self._events):
                return False
            if saved.get("mode", self._mode) != self._mode:
                return False
            if not self._aux_matches(saved.get("aux")):
                return False
            if self._mode == "prompt" and "prompt_boundary" in saved:
                if saved.get("prompt_boundary") != self._prompt_boundary:
                    return False
                if int(saved.get("prompt_events", -1)) != self._prompt_count:
                    return False
                prompt_stat = self._prompt_stat
                if prompt_stat is None:
                    if saved.get("prompt_size") is not None:
                        return False
                else:
                    if int(saved.get("prompt_size", -1)) != prompt_stat[2]:
                        return False
                    if int(saved.get("prompt_mtime_ns", -1)) != prompt_stat[3]:
                        return False
        except (TypeError, ValueError):
            return False
        return True

    def _aux_state(self) -> dict[str, Any]:
        """Compact interpreter aux state carried in the cursor (like the
        JSONL readers): pending results, emitted call count, and flushed
        orphan result ids with bounded overflow."""
        feed = self._feed
        if feed is None:
            return {"pending": [], "emitted": 0, "orphans": []}
        orphans = sorted(feed.orphan_results)
        return {
            "pending": sorted(feed.pending),
            "emitted": len(feed.emitted_calls),
            "orphans": orphans[:500],
            "orphans_truncated": len(orphans) > 500,
        }

    def _aux_matches(self, saved: Any) -> bool:
        if not isinstance(saved, dict):
            return False
        try:
            current = self._aux_state()
            if list(saved.get("pending", [])) != current["pending"]:
                return False
            if int(saved.get("emitted", -1)) != current["emitted"]:
                return False
            if list(saved.get("orphans", [])) != current["orphans"]:
                return False
            if bool(saved.get("orphans_truncated", False)) != current["orphans_truncated"]:
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _export_cursor(self) -> str | None:
        if self._state == "unavailable":
            return None
        if self._file is None:
            dev = ino = size = mtime_ns = None
        else:
            dev, ino, size, mtime_ns = self._file
        prompt_stat = self._prompt_stat
        return _encode_cursor({
            "v": 1,
            "runtime": "cursor",
            "path": self._store_path,
            "session": self._session_id,
            "dev": dev,
            "ino": ino,
            "size": size,
            "mtime_ns": mtime_ns,
            "wal_size": self._wal[2] if self._wal else None,
            "wal_mtime_ns": self._wal[3] if self._wal else None,
            "data_version": self._data_version,
            "max_rowid": self._max_rowid,
            "boundary": self._boundary,
            "events": len(self._events),
            "gen": self._gen,
            "mode": self._mode,
            "prompt_size": prompt_stat[2] if prompt_stat else None,
            "prompt_mtime_ns": prompt_stat[3] if prompt_stat else None,
            "prompt_events": self._prompt_count,
            "prompt_boundary": self._prompt_boundary,
            "aux": self._aux_state(),
        })
