"""Read-only JSONL event protocol with a bounded observation budget by default."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass

from sesskit.envelope import EXIT_ERROR, EXIT_USAGE, ApiError


def encode_record(record: dict) -> str:
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def print_stream_error(exc: ApiError, session_ref: str = "unknown") -> None:
    """Pre-start failures use the same end-event protocol, never a text/envelope switch."""
    import uuid

    from sesskit.streaming import MESSAGE_STREAM_SCHEMA_ID, MessageStreamRecord, message_stream_record_to_dict

    record = MessageStreamRecord(
        schema=MESSAGE_STREAM_SCHEMA_ID, session=session_ref, seq=1, generation=uuid.uuid4().hex,
        mode="history", type="end", state="unavailable", history_state="unavailable", reason=exc.code,
        error={"code": exc.code, "message": exc.message, "hint": exc.hint,
               "next_commands": exc.next_commands},
    )
    print(encode_record(message_stream_record_to_dict(record)), flush=True)


@dataclass
class StreamCommand:
    session: dict
    input_path: str | None
    input_format: str
    interval: float
    polls: int | None
    max_events: int
    cursor: str | None
    duration: float | None
    max_records: int | None
    max_bytes: int | None

    def run(self) -> int:
        from sesskit.stream_source import NativeJSONSource
        from sesskit.streaming import message_stream_record_to_dict, open_message_stream

        with ExitStack() as stack:
            source = None
            if self.input_path:
                try:
                    handle = sys.stdin.buffer if self.input_path == "-" else stack.enter_context(open(self.input_path, "rb"))
                    source = NativeJSONSource(handle, format=self.input_format)
                except (OSError, ValueError, AttributeError):
                    from sesskit.stream_source import NativeEventSource

                    source = NativeEventSource()
                    source.disconnect("source_failed", gap=True)
            stream = stack.enter_context(open_message_stream(
                self.session, live_source=source, cursor=self.cursor, max_events=self.max_events,
            ))
            started = time.monotonic()
            count, written, polls = 0, 0, 0
            # Reserve one end event so a cutoff never silently ends at EOF.
            reserve = 2048 + len(json.dumps(stream.session_ref, ensure_ascii=True))
            reason, code = "poll_limit", 0
            cutoff = False
            try:
                while True:
                    if self.duration is not None and time.monotonic() - started >= self.duration:
                        reason, cutoff = "time_limit", True
                        break
                    for record in stream.poll():
                        line = encode_record(message_stream_record_to_dict(record))
                        size = len(line) + 1  # ASCII encoding makes byte length exact.
                        if self.max_records is not None and count + 1 >= self.max_records:
                            reason, cutoff = "record_limit", True
                            break
                        if self.max_bytes is not None and written + size + reserve > self.max_bytes:
                            reason, cutoff = "byte_limit", True
                            break
                        print(line, flush=True)
                        count += 1
                        written += size
                    polls += 1
                    if cutoff or self.polls is not None and polls >= self.polls:
                        break
                    delay = self.interval
                    if self.duration is not None:
                        delay = min(delay, max(0, self.duration - (time.monotonic() - started)))
                    time.sleep(delay)
                failure = None
                if stream.state in {"unavailable", "unsupported"}:
                    code = EXIT_ERROR
                    failure = {"code": "history_unavailable", "message": "No usable observation source is available.",
                               "hint": "Check the native history or provide a compatible native feed.",
                               "next_commands": ["sesskit describe stream"]}
                end = stream.end(reason, truncated=cutoff, error=failure)
                print(encode_record(message_stream_record_to_dict(end)), flush=True)
                return code
            except BrokenPipeError:
                # This detaches only the reader. An upstream pipe producer owns
                # its transport lifecycle; no agent-control command is sent.
                try:
                    with open(os.devnull, "w") as sink:
                        os.dup2(sink.fileno(), sys.stdout.fileno())
                except (OSError, ValueError):
                    pass
                return 0
            except KeyboardInterrupt:
                end = stream.end("observer_interrupted")
                print(encode_record(message_stream_record_to_dict(end)), flush=True)
                return 130


def cmd_stream(args, registry) -> StreamCommand:
    from sesskit.cli import resolve_ref

    polls = args.polls
    if args.once:
        if polls is not None:
            raise ApiError("usage_error", "--once and --polls are mutually exclusive", EXIT_USAGE)
        polls = 1
    if args.limit < 1 or not 1 <= args.max_events <= 200:
        raise ApiError("usage_error", "--limit must be positive; --max-events must be between 1 and 200", EXIT_USAGE)
    if polls is not None and polls < 1:
        raise ApiError("usage_error", "--polls must be positive", EXIT_USAGE)
    if not math.isfinite(args.interval) or args.interval < 0 or (polls is None and args.interval < 0.01):
        raise ApiError("usage_error", "--interval must be finite and nonnegative (at least 0.01 for following)", EXIT_USAGE)
    if args.input_format is not None and args.input is None:
        raise ApiError("usage_error", "--input-format requires --input", EXIT_USAGE)
    if args.unbounded and any(v is not None for v in (args.duration, args.max_records, args.max_bytes)):
        raise ApiError("usage_error", "--unbounded cannot be combined with observation budget options", EXIT_USAGE)
    duration = args.duration if args.duration is not None else 60.0
    max_records = args.max_records if args.max_records is not None else 1000
    max_bytes = args.max_bytes if args.max_bytes is not None else 262144
    if not math.isfinite(duration) or duration <= 0 or max_records < 2 or max_bytes < 16384:
        raise ApiError("usage_error", "duration must be finite and positive, max-records >= 2, max-bytes >= 16384", EXIT_USAGE)
    session = resolve_ref(registry, args.session, args.limit, include_missing_cwd=args.include_missing_cwd)
    return StreamCommand(session, args.input, args.input_format or "ndjson", args.interval,
                         polls, args.max_events, args.cursor,
                         None if args.unbounded else duration,
                         None if args.unbounded else max_records,
                         None if args.unbounded else max_bytes)
