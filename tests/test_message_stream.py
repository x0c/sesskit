"""Consumer-contract tests: real persisted readers plus native live feed fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sesskit import NativeEventSource, NativeJSONSource, cli, open_message_stream
from sesskit.models import ConversationMessage
from sesskit.stream_native import NativeEventNormalizer
from sesskit.streaming import MESSAGE_STREAM_SCHEMA_ID, message_stream_record_to_dict


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "session.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "session1", "cwd": str(tmp_path)},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": "hello"}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "saved"}], "stopReason": "stop"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path, {"source": "pi", "id": "session1", "path": str(path)}


def apply(records, state):
    for record in records:
        if record.type == "reset":
            state.clear()
        elif record.type == "upsert":
            state[record.key] = record.event
        elif record.type == "remove":
            state.pop(record.key, None)
    return state


def texts(records):
    return [r.event["text"] for r in records if r.type == "upsert" and r.event["type"] == "assistant_message"]


def pi_text(text, typ="message_update"):
    return {"type": typ, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_real_history_follow_append_and_no_duplicate(history):
    path, session = history
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with open_message_stream(session) as stream:
        first = stream.poll()
        assert texts(first) == ["saved"]
        assert first[0].mode == "history"
        assert not stream.poll()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        row = {"type": "message", "id": "a2", "parentId": "a1",
               "message": {"role": "assistant", "content": [{"type": "text", "text": "new"}], "stopReason": "stop"}}
        with path.open("a") as handle:
            handle.write(json.dumps(row))
        assert texts(stream.poll()) == []
        with path.open("a") as handle:
            handle.write("\n")
        updates = stream.poll()
        assert texts(updates) == ["new"]
        assert not any(r.type == "reset" for r in updates)
        assert not stream.poll()


def test_partial_before_final_and_automatic_degradation(history):
    path, session = history
    source = NativeEventSource()
    state = {}
    with open_message_stream(session, live_source=source) as stream:
        assert source.feed({"type": "message_start", "message": {"role": "assistant", "content": []}})
        assert source.feed(pi_text("ear"))
        early = stream.poll()
        assert texts(early) == ["ear"]
        assert any(r.type == "upsert" and r.complete is False for r in early)
        apply(early, state)
        assert source.feed(pi_text("early"))
        assert texts(stream.poll()) == ["early"]
        assert source.feed(pi_text("early", "message_end"))
        final = stream.poll()
        assert texts(final) == ["early"]
        assert any(r.type == "upsert" and r.complete is True for r in final)
        live_generation = final[-1].generation
        source.disconnect()
        degraded = stream.poll()
        apply(degraded, state)
        assert texts(degraded) == ["saved"]
        assert degraded[-1].mode == "history"
        assert degraded[-1].generation != live_generation
        assert degraded[-1].gap
        assert any(r.reason == "source_disconnected" for r in degraded)
        assert all(e["text"] != "early" for e in state.values())
        assert path.read_text().count("saved") == 1


def test_failure_without_store_keeps_visible_data_until_recovery(history):
    path, session = history
    source = NativeEventSource()
    with open_message_stream(session, live_source=source) as stream:
        source.feed(pi_text("visible"))
        state = apply(stream.poll(), {})
        contents = path.read_bytes()
        path.unlink()
        source.disconnect()
        missing = stream.poll()
        assert missing[-1].state == "unavailable"
        assert not any(r.type in {"remove", "reset"} for r in missing)
        assert apply(missing, state)
        path.write_bytes(contents)
        recovered = stream.poll()
        assert any(r.type == "reset" for r in recovered)
        assert texts(recovered) == ["saved"]


def test_callback_overflow_degrades_without_raising(history):
    source = NativeEventSource(capacity=1)
    source.feed(pi_text("accepted"))
    assert not source.feed(pi_text("lost"))
    with open_message_stream(history[1], live_source=source) as stream:
        assert texts(stream.poll()) == ["accepted"]
        fallback = stream.poll()
        assert texts(fallback) == ["saved"]
        assert any(r.reason == "consumer_overflow" and r.gap for r in fallback)
    assert not source.feed(pi_text("after close"))


def test_reconnect_resets_native_state(history):
    source = NativeEventSource()
    with open_message_stream(history[1], live_source=source) as stream:
        source.feed(pi_text("one"))
        first = stream.poll()
        source.disconnect()
        stream.poll()
        source.reconnect()
        source.feed(pi_text("two"))
        second = stream.poll()
        assert any(r.type == "reset" for r in second)
        assert second[-1].generation != first[-1].generation
        assert texts(second) == ["two"]


def test_cursor_rehydrates_bounded_view_and_never_double_appends(history):
    with open_message_stream(history[1], max_events=1) as stream:
        first = stream.poll()
        cursor = first[-1].cursor
    with open_message_stream(history[1], cursor=cursor, max_events=1) as resumed:
        second = resumed.poll()
        assert any(r.type == "reset" for r in second)
        assert texts(second) == ["saved"]
        assert second[-1].seq > first[-1].seq
        state = apply(second, apply(first, {}))
        assert len(state) == 1


def test_history_replacement_resets_generation(history):
    path, session = history
    with open_message_stream(session) as stream:
        first = stream.poll()
        replacement = path.with_suffix(".new")
        replacement.write_text(path.read_text().replace("saved", "replaced"))
        replacement.replace(path)
        second = stream.poll()
        assert any(r.type == "reset" for r in second)
        assert second[-1].generation != first[-1].generation
        assert texts(second) == ["replaced"]


def test_legacy_snapshot_reuses_public_load_and_resets(tmp_path):
    path = tmp_path / "context.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k1", "path": str(path)}
    messages = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]
    with patch("sesskit.streaming.load_session_conversation", return_value=messages) as load, open_message_stream(session) as stream:
        first = stream.poll()
        assert texts(first) == ["answer"]
        stream.poll()
        assert load.call_count == 1
        messages.append(ConversationMessage("assistant", "next"))
        path.write_text("changed file")
        changed = stream.poll()
        assert any(r.type == "reset" for r in changed)
        assert texts(changed) == ["answer", "next"]
        assert all(r.event["message_id"] is None for r in changed if r.type == "upsert")


@pytest.mark.parametrize("value", [{1: "no"}, {"nested": {1: "no"}}, {"x": float("nan")}, {"x": float("inf")},
                                   {"x": ()}, {"x": object()}, [], {"x": "a" * (1 << 20)}])
def test_invalid_native_json_never_escapes_callback(value):
    source = NativeEventSource()
    assert source.feed(value) is False
    batch = source.poll()
    assert not batch.connected and batch.reason == "invalid_event"


def test_cyclic_native_input_refused():
    value = {}
    value["cycle"] = value
    assert not NativeEventSource().feed(value)


def test_queue_copies_producer_input_and_bounds_bytes():
    source = NativeEventSource(max_bytes=30)
    value = {"type": "ping"}
    assert source.feed(value)
    value["type"] = "changed"
    assert source.poll().events == ({"type": "ping"},)
    assert not source.feed({"payload": "x" * 100})
    assert source.poll().reason == "consumer_overflow"


def test_descriptor_input_is_incremental_and_borrowed():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as handle:
        source = NativeJSONSource(handle)
        try:
            os.write(write_fd, b'{"type":"ping"')
            assert not source.poll().events
            os.write(write_fd, b'}\n')
            assert source.poll().events == ({"type": "ping"},)
        finally:
            os.close(write_fd)
        batch = source.poll()
        assert not batch.connected and batch.reason == "source_eof"
        source.close()
        assert os.fstat(handle.fileno())


def test_sse_frame_and_unicode_boundaries(tmp_path):
    path = tmp_path / "events.sse"
    path.write_bytes(': heartbeat\r\n\r\ndata: {"type":"ping",\r\ndata: "text":"你好😀"}\r\n\r\n'.encode())
    with path.open("rb") as handle:
        source = NativeJSONSource(handle, format="sse")
        assert source.poll().events == ({"type": "ping", "text": "你好😀"},)
        assert not source.poll().connected


@pytest.mark.parametrize("runtime,event", [
    ("claude", {"session_id": "other", "type": "assistant", "message": {"content": "secret"}}),
    ("codex", {"method": "item/agentMessage/delta", "params": {"threadId": "other", "itemId": "i", "delta": "secret"}}),
    ("opencode", {"type": "session.text.delta", "data": {"sessionID": "other", "assistantMessageID": "m", "delta": "secret"}}),
    ("kimi", {"session_id": "other", "type": "transcript.ops", "payload": {"ops": []}}),
])
def test_foreign_sessions_are_filtered(runtime, event):
    assert not NativeEventNormalizer(runtime, "s").normalize(event)


def test_claude_partial_final_replay_replaces_and_deduplicates():
    normalizer = NativeEventNormalizer("claude", "s")
    normalizer.normalize({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m"}}})
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": "Hi"}}}
    first = normalizer.normalize(delta)[0]
    final = {"type": "assistant", "message": {"id": "m", "role": "assistant",
                                                   "content": [{"type": "text", "text": "Hi!"}]}}
    second = normalizer.normalize(final)[0]
    assert first.key == second.key
    assert (first.event.text, second.event.text, second.complete) == ("Hi", "Hi!", True)
    assert not normalizer.normalize(final)


def test_cursor_only_new_chunks_and_final_native_error():
    normalizer = NativeEventNormalizer("cursor", "s")
    def event(text, **extra):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, **extra}
    assert normalizer.normalize(event("Hi", timestamp_ms=1))[0].event.text == "Hi"
    assert not normalizer.normalize(event("Hi", timestamp_ms=2, model_call_id="call"))
    assert not normalizer.normalize(event("Hi"))
    assert normalizer.normalize(event("!", timestamp_ms=3))[0].event.text == "Hi!"
    result = normalizer.normalize({"type": "result", "is_error": True, "result": "quota exceeded"})
    assert result[-1].event.error.message == "quota exceeded"
    assert normalizer.outcome.status == "aborted"


def test_codex_completed_item_is_upsert_not_extra_append():
    normalizer = NativeEventNormalizer("codex", "s")
    for text in ("H", "i"):
        result = normalizer.normalize({"method": "item/agentMessage/delta",
                                      "params": {"threadId": "s", "turnId": "t", "itemId": "m", "delta": text}})
    assert result[0].event.text == "Hi"
    result = normalizer.normalize({"method": "item/completed", "params": {"threadId": "s", "turnId": "t",
                                    "item": {"type": "agentMessage", "id": "m", "text": "Hi!"}}})
    assert result[0].event.text == "Hi!" and result[0].complete
    normalizer.normalize({"method": "turn/completed", "params": {"turn": {"id": "t", "status": "failed",
                                                                                                "error": {"message": "failed"}}}})
    assert normalizer.outcome.status == "aborted"


def test_opencode_v2_live_delta_full_end_and_native_dedup():
    normalizer = NativeEventNormalizer("opencode", "s")
    def event(typ, eid, **extra):
        return {"type": "session.text." + typ, "id": eid,
                "data": {"sessionID": "s", "assistantMessageID": "m", "ordinal": 0, **extra}}
    delta = event("delta", "evt_1", delta="Hi")
    assert normalizer.normalize(delta)[0].event.text == "Hi"
    assert not normalizer.normalize(delta)
    final = normalizer.normalize(event("ended", "evt_2", text="Hi!"))[0]
    assert final.event.text == "Hi!" and final.complete


def test_kimi_utf16_offsets_duplicate_append_and_gap():
    normalizer = NativeEventNormalizer("kimi", "s")
    def event(seq, op):
        return {"type": "transcript.ops", "session_id": "s", "payload": {"agent_id": "a", "seq": seq, "ops": [op]}}
    frame = {"op": "frame.upsert", "turnId": "t", "stepId": "step",
             "frame": {"kind": "text", "frameId": "f", "role": "assistant", "text": "😀"}}
    normalizer.normalize(event(1, frame))
    append = {"op": "append", "target": {"frameId": "f"}, "offset": 2, "text": "!"}
    assert normalizer.normalize(event(4, append))[0].event.text == "😀!"
    assert not normalizer.normalize(event(5, append))
    assert not normalizer.normalize(event(5, append))
    with pytest.raises(ValueError, match="gap"):
        normalizer.normalize(event(6, {**append, "offset": 20}))


def test_kimi_reset_replaces_frames_without_duplicate_rows():
    normalizer = NativeEventNormalizer("kimi", "s")
    raw = {"type": "transcript.reset", "session_id": "s", "payload": {"agent_id": "a", "seq": 1,
        "snapshot": {"items": [{"kind": "turn", "turnId": "t", "steps": [{"frames": [
            {"kind": "text", "frameId": "f", "role": "assistant", "text": "one"}]}]}]}}}
    first = normalizer.normalize(raw)
    raw["payload"]["snapshot"]["items"][0]["steps"][0]["frames"][0]["text"] = "two"
    second = normalizer.normalize(raw)
    assert first[0].key == second[-1].key
    assert second[0].operation == "remove" and second[-1].event.text == "two"


def test_native_protocol_failure_degrades_and_does_not_publish_exception_payload(history):
    source = NativeEventSource()
    source.feed({"method": "item/agentMessage/delta", "params": {"delta": "credential: DO-NOT-LOG"}})
    session = {**history[1], "source": "codex"}
    with open_message_stream(session, live_source=source) as stream:
        records = list(stream.poll()) + list(stream.poll())
        assert any(r.reason == "invalid_event" for r in records)
        assert "DO-NOT-LOG" not in json.dumps([asdict(r) for r in records])


def registry(session):
    return SimpleNamespace(get=lambda _runtime: SimpleNamespace(scan_sessions=lambda *_args, **_kwargs: [session]))


def test_cli_once_ndjson_and_describe(history, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:session1", "--once"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["type"] == "end"
    assert rows[-1]["reason"] == "poll_limit"
    assert all(row["schema"] == MESSAGE_STREAM_SCHEMA_ID and row["error"] is None for row in rows)
    assert cli.dispatch(["describe", "stream"]) == 0
    description = json.loads(capsys.readouterr().out)
    assert description["data"]["returns"]["framing"] == "one JSON event per line, including error end events"


@pytest.mark.parametrize("flags", [["--once", "--polls", "1"], ["--polls", "0"], ["--interval", "nan"],
                                   ["--interval", "0"], ["--max-events", "201"], ["--input-format", "sse"]])
def test_cli_rejects_invalid_combinations_as_one_envelope(history, monkeypatch, capsys, flags):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:session1", *flags]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "end"
    assert json.loads(lines[0])["error"]["code"] == "usage_error"


def test_cli_parser_error_is_ndjson(capsys):
    with pytest.raises(SystemExit) as error:
        cli.dispatch(["stream", "pi:s", "--unexpected"])
    assert error.value.code == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["error"]["code"] == "usage_error"


def test_cli_unreadable_input_degrades(history, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:session1", "--input", "/missing/secret-token", "--once"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(row["reason"] == "source_failed" for row in rows)
    assert any(row["event"] and row["event"]["text"] == "saved" for row in rows)
    assert "secret-token" not in str(rows)


def test_bounded_follow_closes_observer_only(history):
    stop = threading.Event()
    source = NativeEventSource()
    stream = open_message_stream(history[1], live_source=source)
    iterator = stream.follow(interval=0.01, stop_event=stop)
    next(iterator)
    stop.set()
    rows = list(iterator)
    assert rows[-1].type == "end" and rows[-1].reason == "observer_stopped"
    assert not source.feed(pi_text("late"))
    with pytest.raises(RuntimeError, match="closed"):
        stream.poll()


def test_dual_schema_and_record_fields(history):
    root = Path(__file__).resolve().parents[1]
    assert (root / "schemas/message-stream.v1.json").read_bytes() == (root / "src/sesskit/schemas/message-stream.v1.json").read_bytes()
    schema = json.loads((root / "schemas/message-stream.v1.json").read_text())
    with open_message_stream(history[1]) as stream:
        for record in stream.poll():
            data = message_stream_record_to_dict(record)
            assert set(data) == set(schema["required"])
            assert data["type"] in schema["properties"]["type"]["enum"]
            assert json.loads(json.dumps(data, allow_nan=False)) == data


def test_foreign_or_unknown_live_events_leave_history_visible(history):
    source = NativeEventSource()
    source.feed({"type": "server_hello"})
    with open_message_stream(history[1], live_source=source) as stream:
        rows = stream.poll()
        assert texts(rows) == ["saved"]
        assert rows[-1].mode == "history"
        assert any(r.reason == "awaiting_native_events" for r in rows)
        source.feed({"type": "message_update", "session_id": "another",
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "foreign"}]}})
        assert not stream.poll()
        source.feed(pi_text("now live"))
        rows = stream.poll()
        assert texts(rows) == ["now live"]
        assert rows[-1].mode == "live" and any(r.type == "reset" for r in rows)


def test_native_fragments_are_coalesced_within_one_poll(history):
    source = NativeEventSource()
    source.feed(pi_text("a"))
    source.feed(pi_text("ab"))
    source.feed(pi_text("abc"))
    with open_message_stream(history[1], live_source=source) as stream:
        assert texts(stream.poll()) == ["abc"]


def test_pi_native_error_survives_agent_end():
    normalizer = NativeEventNormalizer("pi", "s")
    normalizer.normalize({"type": "message_end", "message": {"role": "assistant", "content": [],
                                                                "stopReason": "error", "errorMessage": "rate limited"}})
    normalizer.normalize({"type": "agent_end"})
    assert normalizer.outcome.status == "aborted"
    assert normalizer.outcome.error.message == "rate limited"


def test_fragment_without_baseline_declares_possible_gap(history):
    source = NativeEventSource()
    source.feed({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "suffix"}})
    with open_message_stream(history[1], live_source=source) as stream:
        rows = stream.poll()
        assert texts(rows) == ["suffix"]
        assert all(r.gap for r in rows)


def test_non_json_history_never_commits_invalid_cursor(history, monkeypatch):
    from sesskit.activity_reader import PollResult
    from sesskit.models import ActivityEvent, Evidence, SessionOutcome

    fake = SimpleNamespace(poll=lambda: PollResult(
        events=(ActivityEvent(1, "tool_result", Evidence("native"), raw_output={"bad": float("nan")}),),
        reset=True, generation="bad", cursor="must-not-commit", state="available",
        outcome=SessionOutcome("unknown", Evidence("unknown")),
    ))
    monkeypatch.setattr("sesskit.streaming.open_activity_reader", lambda *_a, **_k: fake)
    with open_message_stream(history[1]) as stream:
        rows = stream.poll()
        assert rows[-1].state == "unavailable"
        assert not any(r.type == "upsert" for r in rows)
        cursor = json.loads(rows[-1].cursor)
        assert cursor["history_cursor"] is None
        json.dumps([asdict(r) for r in rows], allow_nan=False)


@pytest.mark.parametrize("flags,reason", [(["--max-records", "3"], "record_limit"),
                                          (["--duration", "0.01", "--interval", "0.01"], "time_limit")])
def test_cli_observation_budget_always_ends(history, monkeypatch, capsys, flags, reason):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:session1", *flags]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["type"] == "end" and rows[-1]["reason"] == reason
    assert rows[-1]["truncated"] is True and rows[-1]["error"] is None
    if reason == "record_limit":
        assert len(rows) == 3


def test_cli_byte_budget_does_not_split_message_or_json(history, monkeypatch, capsys):
    path, session = history
    path.write_text(path.read_text().replace("saved", "x" * 20000))
    monkeypatch.setattr(cli, "default_registry", lambda: registry(session))
    assert cli.dispatch(["stream", "pi:session1", "--once", "--max-bytes", "16384"]) == 0
    output = capsys.readouterr().out
    rows = [json.loads(line) for line in output.splitlines()]
    assert len(output.encode()) <= 16384
    assert rows[-1]["reason"] == "byte_limit" and rows[-1]["truncated"]
    assert not any(r["event"] and len(r["event"].get("text") or "") > 100 for r in rows)


def test_cli_unbounded_is_explicit_and_not_ignored(history, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:session1", "--once", "--unbounded"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["reason"] == "poll_limit"
    assert cli.dispatch(["stream", "pi:session1", "--unbounded", "--duration", "1"]) == 2
    assert json.loads(capsys.readouterr().out)["type"] == "end"


def test_cli_not_found_uses_end_protocol(history, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_registry", lambda: registry(history[1]))
    assert cli.dispatch(["stream", "pi:missing", "--once"]) == 3
    row = json.loads(capsys.readouterr().out)
    assert row["type"] == "end" and row["error"]["code"] == "not_found"


def test_native_source_concurrent_callbacks():
    source = NativeEventSource(capacity=300)
    accepted = []
    def produce(prefix):
        for index in range(100):
            accepted.append(source.feed({"type": "ping", "id": f"{prefix}:{index}"}))
    threads = [threading.Thread(target=produce, args=(prefix,)) for prefix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = source.poll().events
    assert all(accepted) and len(events) == len({e["id"] for e in events}) == 200
    source.close()


def test_opencode_v1_keeps_user_role_and_requires_delta_baseline():
    normalizer = NativeEventNormalizer("opencode", "s")
    delta = {"type": "message.part.delta", "properties": {
        "sessionID": "s", "messageID": "u", "partID": "p", "field": "text", "delta": "not assistant"}}
    assert not normalizer.normalize(delta)
    assert normalizer.gap
    normalizer.normalize({"type": "message.updated", "properties": {
        "info": {"sessionID": "s", "id": "u", "role": "user"}}})
    result = normalizer.normalize({"type": "message.part.updated", "properties": {
        "part": {"sessionID": "s", "messageID": "u", "id": "p", "type": "text", "text": "question"}}})
    assert result[0].event.type == "user_message"
    assert not normalizer.normalize(delta)


def test_pi_end_without_success_evidence_is_unknown():
    normalizer = NativeEventNormalizer("pi", "s")
    normalizer.normalize({"type": "agent_end"})
    assert normalizer.outcome.status == "unknown"
    normalizer.normalize({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "answer"}], "stopReason": "stop"}})
    normalizer.normalize({"type": "agent_end"})
    assert normalizer.outcome.status == "done"


def test_streaming_guide_python_examples_execute(history):
    root = Path(__file__).resolve().parents[1]
    guide = root / "docs/MESSAGE_STREAMING_GUIDE.md"
    snippets = re.findall(r"```python\n(.*?)```", guide.read_text(), flags=re.DOTALL)
    script = history[0].parent / "consumer.py"
    preamble = ("import sesskit\nfrom types import SimpleNamespace\n"
                f"fixture = {history[1]!r}\n"
                "sesskit.get_adapter = lambda _: SimpleNamespace(scan=lambda **kw: [fixture])\n")
    script.write_text(preamble + "\n".join(snippets))
    result = subprocess.run(
        [sys.executable, str(script)], cwd=script.parent,
        env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(script.parent),
             "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "saved" in result.stdout and "Still working" in result.stdout


@pytest.fixture(params=["pi", "claude", "codex", "cursor", "opencode"])
def runtime_history(request, history, tmp_path):
    from test_activity_reader_jsonl import _claude_assistant, _claude_user, _codex_assistant, _codex_user
    from test_activity_reader_sqlite import _cursor_assistant, _cursor_user, _cursor_write, _v1_db

    runtime = request.param
    if runtime == "pi":
        return history
    path = (tmp_path / "chat" / "store.db" if runtime == "cursor"
            else tmp_path / (runtime + (".db" if runtime == "opencode" else ".jsonl")))
    if runtime in {"claude", "codex"}:
        rows = ([_claude_user("question"), _claude_assistant("answer")] if runtime == "claude"
                else [_codex_user("question"), _codex_assistant("answer")])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif runtime == "cursor":
        _cursor_write(path, [_cursor_user("question"), _cursor_assistant("answer")])
    else:
        _v1_db(path, "s", [
            ("u", {"role": "user", "time": {"created": 1000}}, [("p1", {"type": "text", "text": "question"})]),
            ("a", {"role": "assistant", "time": {"created": 2000, "completed": 3000}},
             [("p2", {"type": "text", "text": "answer"})]),
        ])
    return path, {"source": runtime, "id": "s", "path": str(path)}


def test_all_readers_recover_identical_source_after_unavailable(runtime_history):
    from sesskit import open_activity_reader

    path, session = runtime_history
    reader = open_activity_reader(session)
    first = reader.poll()
    assert first.state == "available" and first.events
    original = path.stat()
    parked = path.with_name(path.name + ".parked")
    path.rename(parked)
    assert reader.poll().state == "unavailable"
    parked.rename(path)
    restored = path.stat()
    assert (restored.st_ino, restored.st_size, restored.st_mtime_ns) == (
        original.st_ino, original.st_size, original.st_mtime_ns)
    recovered = reader.poll()
    assert recovered.state == "available" and recovered.reset
    assert recovered.generation != first.generation
    assert [(e.type, e.text) for e in recovered.events] == [(e.type, e.text) for e in first.events]


def test_unavailable_cursor_cannot_resume_as_available_without_rebuild(runtime_history):
    from sesskit import open_activity_reader

    path, session = runtime_history
    reader = open_activity_reader(session)
    first = reader.poll()
    assert first.state == "available"
    parked = path.with_name(path.name + ".parked")
    path.rename(parked)
    missing = reader.poll()
    assert missing.state == "unavailable"
    parked.rename(path)
    restored = open_activity_reader(session, cursor=missing.cursor).poll()
    assert restored.state == "available" and restored.events
