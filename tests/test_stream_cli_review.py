"""MS-07 regression tests: pre-start stream session attribution.

Argparse-level ``sesskit stream`` errors must attribute an unambiguous session
ref while respecting registered option arity (--option=value, boolean flags,
``--``). Option values (``--max-events 10``, ``--input`` paths, opaque
``--cursor`` JSON) must never leak into the session field; genuinely
ambiguous forms stay ``unknown``. Uses stub registries/synthetic files only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sesskit import cli


def _stub_registry(session):
    return SimpleNamespace(
        get=lambda _runtime: SimpleNamespace(
            scan_sessions=lambda *_args, **_kwargs: [session]
        )
    )


def _history_session(tmp_path):
    path = tmp_path / "session.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "session1", "cwd": str(tmp_path)},
        {"type": "message", "id": "u1", "parentId": None,
         "message": {"role": "user", "content": "hello"}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "saved"}],
                     "stopReason": "stop"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return {"source": "pi", "id": "session1", "path": str(path)}


def _stream_error(argv):
    """Run dispatch expecting an argparse-level SystemExit(2); return (code, record, raw)."""
    with pytest.raises(SystemExit) as exc:
        cli.dispatch(argv)
    return exc.value.code, exc


def _only_stream_end(capsys):
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    record = json.loads(lines[0])
    assert record["type"] == "end"
    return record, out


def test_session_first_unknown_option(capsys):
    code, _ = _stream_error(["stream", "pi:abc", "--bogus-flag"])
    assert code == 2
    record, _ = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert record["error"]["code"] == "usage_error"


def test_options_first_unknown_option(capsys):
    code, _ = _stream_error(["stream", "--once", "pi:abc", "--bogus-flag"])
    assert code == 2
    record, _ = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert record["error"]["code"] == "usage_error"


def test_value_options_before_ref_are_not_sessions(capsys):
    code, _ = _stream_error(["stream", "--max-events", "10", "pi:abc", "--bogus"])
    assert code == 2
    record, _raw = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert "10" not in json.dumps(record.get("session"))


def test_input_path_before_ref_is_not_session(capsys):
    code, _ = _stream_error(
        ["stream", "--input", "/tmp/some-path", "pi:abc", "--bogus"])
    assert code == 2
    record, raw = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert "/tmp/some-path" not in raw


def test_cursor_json_before_ref_never_leaks(capsys):
    secret = '{"private":"secret-token-xyz","seq":99}'
    code, _ = _stream_error(
        ["stream", "--cursor", secret, "pi:abc", "--bogus"])
    assert code == 2
    record, raw = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert "secret-token-xyz" not in raw


def test_equals_form_value_is_not_session(capsys):
    code, _ = _stream_error(
        ["stream", "--max-events=10", "pi:abc", "--bogus"])
    assert code == 2
    record, _ = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"


def test_double_dash_session_with_stub_registry(tmp_path, monkeypatch, capsys):
    session = _history_session(tmp_path)
    monkeypatch.setattr(cli, "default_registry", lambda: _stub_registry(session))
    assert cli.dispatch(["stream", "--once", "--", "pi:session1"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows and all(row["session"] == "pi:session1" for row in rows)
    assert rows[-1]["type"] == "end"


def test_missing_ref_stays_unknown(capsys):
    for argv in (["stream", "--once"], ["stream"], ["stream", "--max-events", "10"],
                 ["stream", "--cursor", '{"a":1}'], ["stream", "--bogus"]):
        code, _ = _stream_error(list(argv))
        assert code == 2, argv
        record, raw = _only_stream_end(capsys)
        assert record["session"] == "unknown", argv
        assert "secret" not in raw.replace('{"a":1}', "")


def test_missing_option_value_with_known_ref(capsys):
    code, _ = _stream_error(["stream", "pi:abc", "--max-events"])
    assert code == 2
    record, _ = _only_stream_end(capsys)
    assert record["session"] == "pi:abc"
    assert record["error"]["code"] == "usage_error"


def test_ambiguous_forms_stay_unknown(capsys):
    for argv in (["stream", "foo", "bar"],
                 ["stream", "--bogus", "pi:abc"],
                 ["stream", "foo", "--bogus", "bar"],
                 ["stream", "--", "foo", "bar"]):
        code, _ = _stream_error(list(argv))
        assert code == 2, argv
        record, _ = _only_stream_end(capsys)
        assert record["session"] == "unknown", argv


def test_non_stream_envelope_unchanged(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.dispatch(["list", "--bogus"])
    assert exc.value.code == 2
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert "session" not in payload


def test_describe_stream_still_lists_session(capsys):
    assert cli.dispatch(["describe", "stream"]) == 0
    payload = json.loads(capsys.readouterr().out)
    flags = [flag for arg in payload["data"]["args"] for flag in arg["flags"]]
    assert "session" in flags
    assert "--once" in flags and "--cursor" in flags


def test_stub_registry_once_success_session_first_and_options_first(
        tmp_path, monkeypatch, capsys):
    session = _history_session(tmp_path)
    monkeypatch.setattr(cli, "default_registry", lambda: _stub_registry(session))
    assert cli.dispatch(["stream", "pi:session1", "--once"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["type"] == "end" and rows[-1]["session"] == "pi:session1"
    assert cli.dispatch(["stream", "--once", "pi:session1"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["type"] == "end" and rows[-1]["session"] == "pi:session1"
