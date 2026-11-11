"""Batched process-metadata reads preserve liveness evidence and key boundaries."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from sesskit.parsers import common


@pytest.fixture(autouse=True)
def clear_process_caches():
    common.clear_live_cwd_cache()
    yield
    common.clear_live_cwd_cache()


def test_command_line_reuses_scan_ps_table_without_per_pid_fork():
    table = "123 /usr/bin/pi --session abc\n456 /usr/bin/opencode run\n"
    with mock.patch.object(common.sys, "platform", "darwin"), \
         mock.patch.object(common, "_ps_axo_pid_command", return_value=table), \
         mock.patch.object(common.subprocess, "check_output") as check:
        assert common.process_command_line(123) == "/usr/bin/pi --session abc"
        assert common.process_command_line(999) == ""
    check.assert_not_called()


def test_command_line_falls_back_to_single_pid_when_table_unavailable():
    with mock.patch.object(common.sys, "platform", "darwin"), \
         mock.patch.object(common, "_ps_axo_pid_command", return_value=""), \
         mock.patch.object(common.subprocess, "check_output", return_value=b"/usr/bin/pi\n") as check:
        assert common.process_command_line(123) == "/usr/bin/pi"
    check.assert_called_once_with(["ps", "-p", "123", "-o", "command="], stderr=subprocess.DEVNULL)


def test_start_times_use_one_ps_call_and_shared_clock():
    with mock.patch.object(common.time, "time", return_value=1_000_000.0), \
         mock.patch.object(
             common.subprocess, "check_output", return_value=b"123 01:02:03\n456 2-03:04:05\n999 nope\n",
         ) as check:
        result = common.process_start_times([123, 456, 999])
    assert result == {123: 996_277.0, 456: 816_155.0}
    check.assert_called_once_with(["ps", "-p", "123,456,999", "-o", "pid=,etime="], stderr=subprocess.DEVNULL)


def test_start_time_batch_failure_does_not_fork_per_pid():
    with mock.patch.object(common.subprocess, "check_output", side_effect=OSError("ps unavailable")) as check:
        assert common.process_start_times([123, 456]) == {}
    check.assert_called_once()


def test_environ_batch_segments_pids_and_filters_undeclared_keys():
    output = (
        b"101 /usr/bin/pi SESSKIT_SESSION_ID=sess-a OTHER=do-not-read\n"
        b"202 /usr/bin/opencode SESSKIT_SESSION_ID=sess-b FAKE_HOST_KEY=host-b OTHER=do-not-read\n"
    )
    with mock.patch.object(common.sys, "platform", "darwin"), \
         mock.patch.object(common.subprocess, "check_output", return_value=output) as check:
        result = common.process_environs([101, 202], extra_keys=("FAKE_HOST_KEY",))
    assert result[101] == {"SESSKIT_SESSION_ID": "sess-a"}
    assert result[202] == {"SESSKIT_SESSION_ID": "sess-b", "FAKE_HOST_KEY": "host-b"}
    assert "OTHER" not in result[101] and "OTHER" not in result[202]
    check.assert_called_once_with(
        ["ps", "eww", "-p", "101,202", "-o", "pid=,command="],
        stderr=subprocess.DEVNULL,
    )


def test_environ_batch_failure_degrades_each_pid_to_empty():
    with mock.patch.object(common.sys, "platform", "darwin"), \
         mock.patch.object(common.subprocess, "check_output", side_effect=OSError("ps unavailable")) as check:
        assert common.process_environs([101, 202]) == {101: {}, 202: {}}
    check.assert_called_once()


def test_environ_batch_uses_cache_and_fetches_only_missing_pids():
    cached = {
        "SESSKIT_SESSION_ID": "cached-a",
        "SESSKIT_RUNTIME": "pi",
        "PI_CODING_AGENT_SESSION_DIR": "/tmp/pi-sessions",
    }
    common._PROC_ENVIRON_CACHE[101] = cached
    output = b"202 /usr/bin/pi SESSKIT_SESSION_ID=sess-b\n"
    with mock.patch.object(common.sys, "platform", "darwin"), \
         mock.patch.object(common.subprocess, "check_output", return_value=output) as check:
        result = common.process_environs([101, 202])
    assert result == {101: cached, 202: {"SESSKIT_SESSION_ID": "sess-b"}}
    check.assert_called_once_with(
        ["ps", "eww", "-p", "202", "-o", "pid=,command="],
        stderr=subprocess.DEVNULL,
    )
