from sesskit.models import effective_session_time, make_session_info, session_key
from sesskit.transcript import SCHEMA_ID, LEGACY_SCHEMA_IDS


def test_effective_session_time_stale():
    mtime, src = effective_session_time(1_000_000.0 + 10_000, 1_000_000.0)
    assert src == "event_time_stale_mtime"
    assert mtime == 1_000_000.0


def test_session_key():
    s = make_session_info(
        source="claude",
        id="x",
        short_id="x",
        cwd="/a",
        mtime=1.0,
        time_source="file_mtime",
        event_time=1.0,
        file_mtime=1.0,
        size_bytes=1,
        native_title=None,
        fallback_title="t",
        status_tag="",
        path="/a.jsonl",
    )
    assert session_key(s) == "claude:x"


def test_schema_id():
    assert SCHEMA_ID == "sesskit.transcript/v1"
    assert "corral.share/v1" in LEGACY_SCHEMA_IDS
