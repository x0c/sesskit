"""Regressions for MS-01 (Claude per-block finals) and MS-06 (Kimi items.remove).

MS-01: the Agent SDK emits one final per content block sharing one message id
(https://code.claude.com/docs/en/agent-sdk/streaming-output). Single-block
finals must land on their evidenced message-global stream index instead of
re-enumerating from zero and clobbering earlier blocks.

MS-06: a malformed Kimi items.remove ids container must raise before mutating
any display row; the stream then degrades via the existing invalid_event path.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sesskit import NativeEventSource, open_message_stream
from sesskit.models import ConversationMessage
from sesskit.stream_native import NativeEventNormalizer


def _start(normalizer, mid="m", uuid="u-start"):
    return normalizer.normalize({"uuid": uuid, "type": "stream_event",
                                 "event": {"type": "message_start", "message": {"id": mid}}})


def _block_start(normalizer, index, block, uuid):
    return normalizer.normalize({"uuid": uuid, "type": "stream_event",
                                 "event": {"type": "content_block_start", "index": index,
                                           "content_block": block}})


def _final(normalizer, mid, content, uuid):
    return normalizer.normalize({"uuid": uuid, "type": "assistant",
                                 "message": {"id": mid, "role": "assistant", "content": content}})


def test_ms01_text_then_tool_finals_keep_both_keys():
    normalizer = NativeEventNormalizer("claude", "s")
    _start(normalizer)
    _block_start(normalizer, 0, {"type": "text", "text": "Hello"}, "u-b0")
    first = _final(normalizer, "m", [{"type": "text", "text": "Hello"}], "u-f0")
    assert [u.key for u in first] == ["m:0"]
    _block_start(normalizer, 1, {"type": "tool_use", "id": "tool-u", "name": "Bash",
                                 "input": {}}, "u-b1")
    second = _final(normalizer, "m", [{"type": "tool_use", "id": "tool-u", "name": "Bash",
                                       "input": {"command": "ls"}}], "u-f1")
    assert [u.key for u in second] == ["m:1"]
    values = normalizer._values
    assert set(values) == {"m:0", "m:1"}
    text, tool = values["m:0"], values["m:1"]
    assert (text.event.type, text.event.text, text.complete) == ("assistant_message", "Hello", True)
    assert (tool.event.type, tool.event.name, tool.event.call_id, tool.complete) == (
        "tool_call", "Bash", "tool-u", True)
    assert tool.event.raw_input == {"command": "ls"}


def test_ms01_tool_then_text_finals():
    normalizer = NativeEventNormalizer("claude", "s")
    _start(normalizer)
    _block_start(normalizer, 0, {"type": "tool_use", "id": "tool-u", "name": "Bash",
                                 "input": {}}, "u-b0")
    first = _final(normalizer, "m", [{"type": "tool_use", "id": "tool-u", "name": "Bash",
                                      "input": {"command": "ls"}}], "u-f0")
    assert [u.key for u in first] == ["m:0"]
    _block_start(normalizer, 1, {"type": "text", "text": "done"}, "u-b1")
    second = _final(normalizer, "m", [{"type": "text", "text": "done"}], "u-f1")
    assert [u.key for u in second] == ["m:1"]
    values = normalizer._values
    assert values["m:0"].event.type == "tool_call"
    assert (values["m:1"].event.type, values["m:1"].event.text) == ("assistant_message", "done")


def test_ms01_whole_message_array_final_and_replay_unchanged():
    normalizer = NativeEventNormalizer("claude", "s")
    updates = _final(normalizer, "m", [{"type": "text", "text": "A"},
                                       {"type": "tool_use", "id": "t1", "name": "Bash",
                                        "input": {"command": "ls"}}], "u-full")
    assert [u.key for u in updates] == ["m:0", "m:1"]
    assert updates[0].event.type == "assistant_message"
    assert updates[1].event.type == "tool_call"
    assert _final(normalizer, "m", [{"type": "text", "text": "A"},
                                    {"type": "tool_use", "id": "t1", "name": "Bash",
                                     "input": {"command": "ls"}}], "u-full-replay") == []


def test_ms01_duplicate_per_block_final_is_deduped():
    normalizer = NativeEventNormalizer("claude", "s")
    _start(normalizer)
    _block_start(normalizer, 1, {"type": "tool_use", "id": "tool-u", "name": "Bash",
                                 "input": {}}, "u-b1")
    first = _final(normalizer, "m", [{"type": "tool_use", "id": "tool-u", "name": "Bash",
                                      "input": {"command": "ls"}}], "u-f1")
    assert [u.key for u in first] == ["m:1"]
    assert _final(normalizer, "m", [{"type": "tool_use", "id": "tool-u", "name": "Bash",
                                     "input": {"command": "ls"}}], "u-f1-replay") == []


def test_ms01_pi_cumulative_array_still_enumerates_from_zero():
    normalizer = NativeEventNormalizer("pi", "s")
    content = [{"type": "text", "text": "a"},
               {"type": "toolCall", "id": "c1", "name": "Bash", "arguments": {"command": "ls"}}]
    partial = normalizer.normalize({"type": "message_update",
                                    "message": {"role": "assistant", "content": content}})
    assert [u.key for u in partial] == ["local:1:0", "local:1:1"]
    assert all(u.complete is False for u in partial)
    final = normalizer.normalize({"type": "message_end",
                                  "message": {"role": "assistant", "content": content,
                                              "stopReason": "stop"}})
    assert [u.key for u in final] == ["local:1:0", "local:1:1"]
    assert all(u.complete is True for u in final)


def _apply(records, state):
    for record in records:
        if record.type == "reset":
            state.clear()
        elif record.type == "upsert":
            state[record.key] = (record.event, record.complete)
        elif record.type == "remove":
            state.pop(record.key, None)
    return state


def test_ms01_consumer_retains_hello_and_completed_tool(tmp_path):
    from test_activity_reader_jsonl import _claude_assistant, _claude_user
    path = tmp_path / "claude.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in (
        _claude_user("question"), _claude_assistant("old"))))
    session = {"source": "claude", "id": "s", "path": str(path)}
    source = NativeEventSource()
    state = {}
    with open_message_stream(session, live_source=source) as stream:
        _apply(stream.poll(), state)
        assert source.feed({"uuid": "c0", "type": "stream_event",
                            "event": {"type": "message_start", "message": {"id": "m"}}})
        assert source.feed({"uuid": "c1", "type": "stream_event",
                            "event": {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": "Hello"}}})
        assert source.feed({"uuid": "c2", "type": "assistant",
                            "message": {"id": "m", "role": "assistant",
                                        "content": [{"type": "text", "text": "Hello"}]}})
        _apply(stream.poll(), state)
        assert source.feed({"uuid": "c3", "type": "stream_event",
                            "event": {"type": "content_block_start", "index": 1,
                                      "content_block": {"type": "tool_use", "id": "tool-u",
                                                        "name": "Bash", "input": {}}}})
        assert source.feed({"uuid": "c4", "type": "assistant",
                            "message": {"id": "m", "role": "assistant",
                                        "content": [{"type": "tool_use", "id": "tool-u",
                                                     "name": "Bash",
                                                     "input": {"command": "ls"}}]}})
        _apply(stream.poll(), state)
    text_rows = [(key, event["type"], event.get("text"), complete)
                 for key, (event, complete) in state.items()]
    assert ("m:0", "assistant_message", "Hello", True) in text_rows
    tool_rows = [(key, event["type"], event.get("name"), event.get("raw_input"), complete)
                 for key, (event, complete) in state.items()]
    assert ("m:1", "tool_call", "Bash", {"command": "ls"}, True) in tool_rows
    assert not [row for row in text_rows if row[0] == "m:0" and row[1] == "tool_call"]


def _kimi_ops(agent="a", seq=1, ops=(), sid="s"):
    return {"type": "transcript.ops", "session_id": sid,
            "payload": {"agent_id": agent, "seq": seq, "ops": list(ops)}}


def _kimi_text(frame_id, text, turn):
    return {"op": "frame.upsert", "turnId": turn, "stepId": "t.1",
            "frame": {"kind": "text", "frameId": frame_id, "role": "assistant", "text": text}}


def _seed_kimi():
    normalizer = NativeEventNormalizer("kimi", "s")
    normalizer.normalize(_kimi_ops(seq=1, ops=[_kimi_text("f-b", "bee", "b")]))
    normalizer.normalize(_kimi_ops(seq=2, ops=[_kimi_text("f-c", "see", "c")]))
    return normalizer


@pytest.mark.parametrize("bad_ids", ["abc", {"b": 1}, None, ["b", 5], [["b"]], [b"b"], [None]])
def test_ms06_malformed_ids_raise_without_removal(bad_ids):
    normalizer = _seed_kimi()
    before = dict(normalizer._values)
    with pytest.raises(ValueError):
        normalizer.normalize(_kimi_ops(seq=3, ops=[{"op": "items.remove", "ids": bad_ids}]))
    assert set(normalizer._values) == set(before)
    assert sorted(v.event.text for v in normalizer._values.values()) == ["bee", "see"]


def test_ms06_valid_empty_is_noop_and_exact_removal_is_scoped():
    normalizer = _seed_kimi()
    normalizer.normalize(_kimi_ops(agent="x", seq=1, ops=[_kimi_text("f-z", "zed", "b")]))
    assert normalizer.normalize(_kimi_ops(seq=3, ops=[{"op": "items.remove", "ids": []}])) == []
    assert len(normalizer._values) == 3
    removed = normalizer.normalize(_kimi_ops(seq=4, ops=[{"op": "items.remove", "ids": ["b"]}]))
    assert {u.key for u in removed if u.operation == "remove"} == {"kimi:a:f-b"}
    assert set(normalizer._values) == {"kimi:a:f-c", "kimi:x:f-z"}
    assert normalizer.normalize(_kimi_ops(seq=2, ops=[_kimi_text("f-old", "old", "c")])) == []
    assert len(normalizer.normalize(_kimi_ops(seq=5, ops=[_kimi_text("f-new", "new", "c")]))) == 1


def test_ms06_malformed_remove_degrades_to_history(tmp_path):
    path = tmp_path / "context.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k1", "path": str(path)}
    messages = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]
    source = NativeEventSource()
    with patch("sesskit.streaming.load_session_conversation",
               return_value=messages), open_message_stream(session, live_source=source) as stream:
        first = stream.poll()
        assert [r.event["text"] for r in first
                if r.type == "upsert" and r.event["type"] == "assistant_message"] == ["answer"]
        assert source.feed(_kimi_ops(seq=1, ops=[_kimi_text("f-live", "live", "t")], sid="k1"))
        live = stream.poll()
        assert live[-1].mode == "live"
        assert [r.event["text"] for r in live
                if r.type == "upsert" and r.event["type"] == "assistant_message"] == ["live"]
        assert source.feed(_kimi_ops(seq=2, ops=[{"op": "items.remove", "ids": "abc"}], sid="k1"))
        degraded = stream.poll()
        assert any(r.reason == "invalid_event" for r in degraded)
        assert any(r.gap for r in degraded)
        assert degraded[-1].mode == "history"
        assert [r.event["text"] for r in degraded
                if r.type == "upsert" and r.event["type"] == "assistant_message"] == ["answer"]
