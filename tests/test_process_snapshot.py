"""进程快照数据源回归：ps 表精确匹配必须覆盖 pgrep 精确匹配。

背景：``_pids_for_process_name`` 在 darwin 下用同波次 ``ps`` 全表做精确匹配
（一次 fork 约 36ms 供 6 个运行时共用），代替 6 次 pgrep fork（约 120ms）。
comm 被改掉的进程（agent→MainThread、pi→node）走 cmdline 兜底，不在本用例
范围内。本用例断言：ps 表挑出的 pid 必须覆盖 pgrep 挑出的 pid（无遗漏）。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from sesskit.parsers.common import (
    _exact_pids_from_ps_table,
    _pids_for_process_name,
    _pids_matching_cmdline,
    _ps_axo_pid_command,
    clear_ps_cmdline_cache,
    is_cursor_agent_cmdline,
    is_pi_cmdline,
)


@unittest.skipUnless(sys.platform == "darwin", "ps 表复用只在 darwin 生效")
class ProcessSnapshotTests(unittest.TestCase):
    def test_ps_table_covers_pgrep_exact_match(self):
        clear_ps_cmdline_cache()
        table = _ps_axo_pid_command()
        self.assertTrue(table.strip(), "本机应能读到 ps 全表")
        for name in ("codex", "agent", "opencode", "kimi", "pi", "claude"):
            with self.subTest(process=name):
                from_ps = set(_exact_pids_from_ps_table(name, table) or [])
                try:
                    raw = subprocess.check_output(
                        ["pgrep", "-x", name], stderr=subprocess.DEVNULL
                    ).decode().split()
                except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                    raw = []
                from_pgrep = {int(x) for x in raw if x.isdigit()}
                self.assertFalse(
                    from_pgrep - from_ps,
                    f"ps 表漏掉了 pgrep 能找到的 {name} 进程",
                )

    def test_cmdline_fallback_still_finds_renamed_processes(self):
        # comm 被改掉的主进程必须仍被兜底找到（若本机没有则跳过断言数量）。
        agent_pids = _pids_matching_cmdline(is_cursor_agent_cmdline)
        pi_pids = _pids_matching_cmdline(is_pi_cmdline)
        for pid in (*agent_pids, *pi_pids):
            self.assertGreater(pid, 0)
        # _pids_for_process_name 必须包含兜底结果。
        for name, fallback in (("agent", agent_pids), ("pi", pi_pids)):
            with self.subTest(process=name):
                merged = set(_pids_for_process_name(name))
                self.assertTrue(
                    set(fallback) <= merged,
                    f"{name} 的 cmdline 兜底结果在合并后丢失",
                )

    def test_empty_table_falls_back_without_crash(self):
        # 空表返回 None，调用方回落 pgrep。
        self.assertIsNone(_exact_pids_from_ps_table("codex", ""))
        # 非空但无命中行返回空列表（调用方直接用，不再回落；agent/pi 的
        # cmdline 兜底仍在 _pids_for_process_name 里照常执行）。
        # "not a table" 切出 argv0 为 a，不等于 codex。
        self.assertEqual(_exact_pids_from_ps_table("codex", "not a table"), [])


if __name__ == "__main__":
    unittest.main()
