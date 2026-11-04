"""MS-04/MS-05 legacy recovery regressions (public poll boundary only)."""
from __future__ import annotations

import os
from unittest.mock import patch

from sesskit import NativeEventSource, open_message_stream
from sesskit.models import ConversationMessage
from sesskit.registry import ConversationLoadError


def _texts(records):
    return [
        r.event["text"]
        for r in records
        if r.type == "upsert" and r.event and r.event.get("type") == "assistant_message"
    ]


def _kimi_live_reset(session_id, text, agent="a", seq=1):
    return {
        "type": "transcript.reset",
        "session_id": session_id,
        "payload": {
            "agent_id": agent,
            "seq": seq,
            "snapshot": {
                "items": [
                    {
                        "kind": "turn",
                        "turnId": "t",
                        "steps": [
                            {"frames": [{"kind": "text", "frameId": "f", "role": "assistant", "text": text}]}
                        ],
                    }
                ]
            },
        },
    }


def test_ms04_transient_stat_fault_recovers_with_reload(tmp_path):
    path = tmp_path / "kimi_ms04.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-ms04", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load) as load,
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        assert first[-1].state == "available"
        gen0 = first[-1].generation
        st0 = os.stat(str(path))
        real_stat = os.stat
        fired = {"n": False}

        def flaky(p, *a, **k):
            if str(p) == str(path) and not fired["n"]:
                fired["n"] = True
                raise OSError("injected transient stat failure")
            return real_stat(p, *a, **k)

        with patch.object(os, "stat", flaky):
            fault = stream.poll()
        assert fault[-1].state == "unavailable"
        assert path.read_text() == "placeholder"
        st1 = os.stat(str(path))
        assert (st1.st_dev, st1.st_ino, st1.st_size, st1.st_mtime_ns, st1.st_ctime_ns) == (
            st0.st_dev,
            st0.st_ino,
            st0.st_size,
            st0.st_mtime_ns,
            st0.st_ctime_ns,
        )
        assert load.call_count == 1
        recovery = stream.poll()
        assert recovery, "MS-04 bug: recovery empty, stuck unavailable"
        assert recovery[-1].state == "available"
        assert any(r.type == "reset" for r in recovery)
        assert recovery[-1].generation != gen0
        assert _texts(recovery) == ["answer"]
        assert load.call_count == 2


def test_ms05_atomic_replacement_identical_resets_without_missing_poll(tmp_path):
    path = tmp_path / "kimi_ms05.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-ms05", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load),
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        assert first[-1].state == "available"
        gen0 = first[-1].generation
        old_stat = os.stat(str(path))
        parked = tmp_path / "kimi_ms05.jsonl.parked"
        os.rename(str(path), str(parked))
        try:
            path.write_text("placeholder")
            new_stat = os.stat(str(path))
            assert new_stat.st_ino != old_stat.st_ino
            second = stream.poll()
        finally:
            try:
                parked.unlink()
            except OSError:
                pass
        assert second, "MS-05 bug: atomic identical replacement returned no reset"
        assert second[-1].state == "available"
        assert any(r.type == "reset" for r in second)
        assert second[-1].generation != gen0
        assert _texts(second) == ["answer"]


def test_ms05_unavailable_then_identical_new_inode_resets(tmp_path):
    path = tmp_path / "kimi_ms05b.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-ms05b", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load),
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        gen0 = first[-1].generation
        old_stat = os.stat(str(path))
        parked = tmp_path / "kimi_ms05b.jsonl.parked"
        os.rename(str(path), str(parked))
        try:
            missing = stream.poll()
            assert missing[-1].state == "unavailable"
            path.write_text("placeholder")
            new_stat = os.stat(str(path))
            assert new_stat.st_ino != old_stat.st_ino
            restored = stream.poll()
        finally:
            try:
                parked.unlink()
            except OSError:
                pass
        assert restored[-1].state == "available"
        assert any(r.type == "reset" for r in restored)
        assert restored[-1].generation != gen0
        assert _texts(restored) == ["answer"]


def test_unchanged_available_poll_stays_cheap(tmp_path):
    path = tmp_path / "kimi_cheap.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-cheap", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load) as load,
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        gen0 = first[-1].generation
        assert load.call_count == 1
        second = stream.poll()
        assert second == ()
        assert stream.state == "available"
        assert load.call_count == 1
        third = stream.poll()
        assert third == ()
        assert load.call_count == 1
        assert stream.state == "available"
        assert first[-1].generation == gen0


def test_metadata_touch_identical_does_not_churn_generation(tmp_path):
    path = tmp_path / "kimi_touch.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-touch", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load) as load,
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        gen0 = first[-1].generation
        assert load.call_count == 1
        st = os.stat(str(path))
        os.utime(str(path), ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        touched = stream.poll()
        assert stream.state == "available"
        assert not any(r.type == "reset" for r in touched)
        # Generation must not churn on identical-values metadata touch.
        assert load.call_count == 2
        gens = {r.generation for r in touched} if touched else set()
        assert not gens or gens == {gen0}


def test_changed_content_resets(tmp_path):
    path = tmp_path / "kimi_changed.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-changed", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load),
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        gen0 = first[-1].generation
        msgs.append(ConversationMessage("assistant", "next"))
        path.write_text("changed file!")
        changed = stream.poll()
        assert any(r.type == "reset" for r in changed)
        assert changed[-1].generation != gen0
        assert _texts(changed) == ["answer", "next"]


def test_loader_failure_stays_unavailable_and_preserves_view(tmp_path):
    path = tmp_path / "kimi_loadfail.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-loadfail", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "answer")]
    calls = {"n": 0}

    def _load(sess):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConversationLoadError("injected transient load failure")
        return list(msgs)

    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load),
        open_message_stream(session) as stream,
    ):
        first = stream.poll()
        assert first[-1].state == "available"
        assert _texts(first) == ["answer"]
        path.write_text("changed bytes here")
        failed = stream.poll()
        assert failed[-1].state == "unavailable"
        assert failed[-1].history_state == "unavailable"
        assert not any(r.type in {"reset", "upsert", "remove"} for r in failed)
        gen0 = first[-1].generation
        recovered = stream.poll()
        assert recovered[-1].state == "available"
        assert any(r.type == "reset" for r in recovered)
        assert recovered[-1].generation != gen0
        assert _texts(recovered) == ["answer"]


def test_live_mode_not_erased_by_background_replacement(tmp_path):
    path = tmp_path / "kimi_live.jsonl"
    path.write_text("placeholder")
    session = {"source": "kimi", "id": "k-live", "path": str(path)}
    msgs = [ConversationMessage("user", "question"), ConversationMessage("assistant", "saved-history")]

    def _load(sess, _msgs=msgs):
        return list(_msgs)

    source = NativeEventSource()
    with (
        patch("sesskit.streaming.load_session_conversation", side_effect=_load),
        open_message_stream(session, live_source=source) as stream,
    ):
        assert source.feed(_kimi_live_reset("k-live", "live-one"))
        live = stream.poll()
        assert live[-1].mode == "live"
        assert "live-one" in _texts(live)
        live_gen = live[-1].generation
        parked = tmp_path / "kimi_live.jsonl.parked"
        os.rename(str(path), str(parked))
        try:
            path.write_text("placeholder")
        finally:
            try:
                parked.unlink()
            except OSError:
                pass
        msgs[:] = [ConversationMessage("user", "question"), ConversationMessage("assistant", "new-history")]
        quiet = stream.poll()
        assert not any(r.type == "reset" for r in quiet)
        if quiet:
            assert quiet[-1].mode == "live"
        assert stream.state == "available"
        assert "new-history" not in _texts(quiet)
        source.disconnect()
        degraded = stream.poll()
        assert degraded[-1].mode == "history"
        assert degraded[-1].state == "available"
        assert any(r.type == "reset" for r in degraded)
        assert degraded[-1].generation != live_gen
        assert _texts(degraded) == ["new-history"]
