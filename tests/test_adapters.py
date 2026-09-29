"""Adapter protocol: registry, capabilities, and dispatch parity per runtime."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sesskit import cli
from sesskit.activity import load_activity, to_v1_dicts
from sesskit.activity_reader import IncrementalUnsupported
from sesskit.adapters import Capabilities, RuntimeAdapter, get_adapter, list_adapters
from sesskit.models import ConversationMessage
from sesskit.parsers import claude, codex, cursor, kimi, opencode, pi
from sesskit.registry import ConversationLoadError, RuntimeParser, default_registry, load_session_conversation
from sesskit.transcript import load_events


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _claude_session(tmp_path):
    path = _write_jsonl(tmp_path / "claude.jsonl", [
        {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hello human"}]}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hello back"}]}},
    ])
    return {"source": "claude", "path": path, "id": "sess-claude"}


def _codex_session(tmp_path):
    path = _write_jsonl(tmp_path / "codex.jsonl", [
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "run it"}]}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "running"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    return {"source": "codex", "path": path, "id": "sess-codex"}


def _kimi_session(tmp_path):
    path = _write_jsonl(tmp_path / "wire.jsonl", [
        {"type": "context.append_message", "time": 1_784_275_205_000,
         "message": {"role": "user", "origin": {"kind": "user"},
                     "content": [{"type": "text", "text": "read the guide"}]}},
        {"type": "context.append_loop_event", "time": 1_784_275_209_000,
         "event": {"type": "content.part",
                   "part": {"type": "text", "text": "done reading"}}},
    ])
    return {"source": "kimi", "path": path, "id": "sess-kimi"}


def _opencode_session(tmp_path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
                 "time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
                 "time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("m_user", "ses_1", 100_000, 100_000,
                  json.dumps({"role": "user", "time": {"created": 100_000}})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("m_asst", "ses_1", 200_000, 200_000,
                  json.dumps({"role": "assistant", "finish": "stop",
                              "time": {"created": 200_000}})))
    for part_id, message_id, text in (("p_user", "m_user", "run tests"),
                                      ("p_text", "m_asst", "on it")):
        role = "user" if message_id == "m_user" else "assistant"
        assert role
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     (part_id, message_id, "ses_1", 100_000, 100_000,
                      json.dumps({"type": "text", "text": text})))
    conn.commit()
    conn.close()
    return {"source": "opencode", "path": str(db), "id": "ses_1"}


def _cursor_session(tmp_path):
    store = tmp_path / "store.db"
    conn = sqlite3.connect(str(store))
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    objects = [
        {"role": "user", "content": "<user_query>fix the icon</user_query>"},
        {"role": "assistant", "content": [{"type": "text", "text": "fixing"}]},
    ]
    for index, obj in enumerate(objects):
        conn.execute("INSERT INTO blobs VALUES (?, ?)",
                     (f"blob-{index:04d}", json.dumps(obj).encode()))
    conn.commit()
    conn.close()
    return {"source": "cursor", "path": str(store), "id": "sess-cursor"}


def _pi_session(tmp_path):
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        {"type": "session", "id": "pi-demo", "timestamp": "2026-01-01T00:00:00Z",
         "cwd": "/tmp/pi-adapter-fixture"},
        {"type": "message", "id": "u1", "parentId": None,
         "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Run checks."}]}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "All green."}],
                     "stopReason": "stop"}},
    ])
    return {"source": "pi", "path": path, "id": "pi-demo"}


BUILDERS = {
    "claude": _claude_session,
    "codex": _codex_session,
    "opencode": _opencode_session,
    "kimi": _kimi_session,
    "cursor": _cursor_session,
    "pi": _pi_session,
}

DIRECT_LOADERS = {
    "claude": lambda s: claude.load_conversation(s["path"]),
    "codex": lambda s: codex.load_conversation(s["path"]),
    "kimi": lambda s: kimi.load_conversation(s["path"]),
    "cursor": lambda s: cursor.load_conversation(s["path"]),
    "pi": lambda s: pi.load_conversation(s["path"]),
    "opencode": lambda s: opencode.load_conversation(s["path"], s["id"]),
}


def test_registry_lists_six_adapters():
    adapters = list_adapters()
    assert [a.id for a in adapters] == ["claude", "codex", "opencode", "kimi", "cursor", "pi"]
    assert all(isinstance(a, RuntimeAdapter) for a in adapters)
    assert all(isinstance(a.capabilities, Capabilities) for a in adapters)
    assert get_adapter("pi").display_name == "Pi"
    with pytest.raises(KeyError, match="unregistered runtime: nope"):
        get_adapter("nope")


def test_capability_declarations():
    caps = {a.id: a.capabilities for a in list_adapters()}
    # Incremental reading is Pi-only; snapshots are the fallback elsewhere.
    assert caps["pi"].incremental_reading is True
    assert all(caps[r].incremental_reading is False for r in caps if r != "pi")
    # Typed activity covers every runtime except Kimi (deferred scope).
    assert caps["kimi"].typed_activity is False
    assert all(caps[r].typed_activity is True for r in caps if r != "kimi")
    # Structured questions only where native shapes are normalized.
    assert caps["claude"].structured_questions is True
    assert caps["codex"].structured_questions is True
    assert caps["cursor"].structured_questions is True
    assert caps["opencode"].structured_questions is True
    assert caps["pi"].structured_questions is False
    assert caps["kimi"].structured_questions is False
    # Native tool-result status only where history marks success/failure.
    assert caps["pi"].native_tool_result_status is True
    assert caps["claude"].native_tool_result_status is True
    assert caps["opencode"].native_tool_result_status is True
    assert caps["codex"].native_tool_result_status is False
    assert caps["cursor"].native_tool_result_status is False
    assert caps["kimi"].native_tool_result_status is False
    # Explicit turn markers drive outcome inference; tail-role-only tails do not.
    assert caps["cursor"].native_turn_boundaries is False
    assert caps["kimi"].native_turn_boundaries is False
    assert caps["pi"].native_turn_boundaries is True
    # Nothing normalized yet for permissions.
    assert all(c.permission_records is False for c in caps.values())
    # Stage G: relations, usage, compaction, and origin are normalized where
    # native history provides evidence (Kimi deferred; cursor has no native
    # usage or compaction markers).
    assert all(caps[r].subagent_linkage is True for r in caps if r != "kimi")
    assert caps["kimi"].subagent_linkage is False
    for runtime in ("claude", "codex", "opencode", "pi"):
        assert caps[runtime].usage_data is True
        assert caps[runtime].compaction_markers is True
        assert caps[runtime].message_origin is True
    assert caps["cursor"].usage_data is False
    assert caps["cursor"].compaction_markers is False
    assert caps["cursor"].message_origin is True
    assert caps["kimi"].usage_data is False
    assert caps["kimi"].compaction_markers is False
    assert caps["kimi"].message_origin is False
    # Error-only turns: declared behind an explicit flag by every
    # typed-activity runtime (loader support is Pi-first today).
    assert caps["kimi"].conversation_include_errors is False
    assert all(caps[r].conversation_include_errors is True for r in caps if r != "kimi")
    # Capabilities stay frozen.
    with pytest.raises(AttributeError):
        caps["pi"].typed_activity = False  # type: ignore[misc]


def test_default_registry_wraps_adapters():
    registry = default_registry()
    assert registry.ids == ("claude", "codex", "opencode", "kimi", "cursor", "pi")
    for runtime_id in registry.ids:
        assert registry.get(runtime_id).adapter is get_adapter(runtime_id)
    with pytest.raises(KeyError, match="unregistered runtime"):
        registry.get("nope")


@pytest.mark.parametrize("runtime_id", sorted(BUILDERS))
def test_conversation_dispatch_parity(tmp_path, runtime_id):
    session = BUILDERS[runtime_id](tmp_path)
    adapter = get_adapter(runtime_id)
    via_adapter = [(m.role, m.text) for m in adapter.load_conversation(session)]
    via_registry = [(m.role, m.text) for m in load_session_conversation(session)]
    via_parser = [(m.role, m.text) for m in default_registry().get(runtime_id).load_conversation(session)]
    via_direct = [(m.role, m.text) for m in DIRECT_LOADERS[runtime_id](session)]
    assert via_adapter == via_registry == via_parser == via_direct
    assert via_adapter, f"{runtime_id} fixture produced no conversation"
    assert all(role in {"user", "assistant"} and text.strip() for role, text in via_adapter)


@pytest.mark.parametrize("runtime_id", sorted(BUILDERS))
def test_activity_and_event_dispatch_parity(tmp_path, runtime_id):
    session = BUILDERS[runtime_id](tmp_path)
    adapter = get_adapter(runtime_id)
    snapshot = adapter.load_activity(session)
    assert snapshot == load_activity(session)
    assert adapter.load_events(session) == load_events(session)
    if adapter.capabilities.typed_activity:
        assert snapshot.state in {"available", "empty"}
        assert to_v1_dicts(snapshot) == load_events(session)
    else:
        assert snapshot.state == "unsupported"
        assert snapshot.events == ()


def test_unknown_runtime_errors_unchanged():
    with pytest.raises(ConversationLoadError, match=r"unregistered runtime: nope"):
        load_session_conversation({"source": "nope", "path": "/tmp/x", "id": "x"})
    with pytest.raises(ConversationLoadError, match="session has no history path"):
        load_session_conversation({"source": "claude", "path": "", "id": "x"})
    with pytest.raises(ConversationLoadError, match="history path not found"):
        load_session_conversation({"source": "codex", "path": "/no/such/file", "id": "x"})
    with pytest.raises(ConversationLoadError, match="opencode session is missing id"):
        load_session_conversation({"source": "opencode", "path": "/tmp/x.db", "id": ""})
    assert load_events({"source": "nope", "path": "/nope"}) == []
    assert load_events({"source": "claude", "path": "/no/such/file.jsonl"}) == []
    snapshot = load_activity({"source": "nope", "path": "/nope", "id": "x"})
    assert snapshot.state == "unsupported"
    assert snapshot.events == ()


def test_open_reader_capability_gate(tmp_path):
    session = _pi_session(tmp_path)
    reader = get_adapter("pi").open_reader(session)
    first = reader.poll()
    assert first.state in {"available", "empty"}
    for runtime_id in ("claude", "codex", "opencode", "kimi", "cursor"):
        with pytest.raises(IncrementalUnsupported):
            get_adapter(runtime_id).open_reader({"source": runtime_id, "path": "", "id": "x"})


def test_signature_matches_parser_signature():
    assert get_adapter("claude").signature() == claude.scan_signature()
    assert get_adapter("pi").signature() == pi.scan_signature()
    assert get_adapter("cursor").signature() == cursor.scan_signature()


def test_legacy_test_double_still_works():
    def load(session):
        return [ConversationMessage(role="user", text="hi", timestamp=1.0)]

    parser = RuntimeParser(id="demo", display_name="Demo",
                           _scan=lambda limit=50: [], _load=load)
    assert parser.adapter is None
    assert [(m.role, m.text) for m in parser.load_conversation({"id": "x"})] == [("user", "hi")]
    assert parser.scan_signature() is None


def test_describe_reports_capabilities(capsys):
    code = cli.dispatch(["describe"])
    assert code == 0
    import json as _json

    out = _json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    runtimes = {r["id"]: r for r in out["data"]["runtimes"]}
    assert set(runtimes) == set(BUILDERS)
    assert runtimes["pi"]["capabilities"]["incremental_reading"] is True
    assert runtimes["kimi"]["capabilities"]["typed_activity"] is False
