"""跨扫描器共享的 helper。

各运行时扫描器互不依赖，但都需要相同的小工具；集中到这里避免多份重复实现。
运行时私有的解析格式仍留在各自的 scan_*.py 里。cwd / 命令行 / 环境按 pid
集合做进程内缓存，避免后台重扫对同一批仍活着的进程反复 fork。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

from sesskit.hosted import PROCESS_ENV_KEYS


def shorten_cwd(cwd: str) -> str:
    """把工作目录路径里的用户主目录前缀替换为 ~，用于列表页展示。"""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def is_ephemeral_agent_cwd(cwd: str) -> bool:
    """OpenConductor 管家等自动任务写在 /tmp/oc-manager-* 下的临时 cwd。

    这类目录会随任务创建/删除反复出现：曾经因「cwd 不存在」被滤掉的旧会话，
    在目录复活后会整批重新进入扫描结果，被 SessionStore 当成「新会话」插到
    列表最前，造成侧边栏被几天前的管家会话刷屏。扫描阶段直接丢弃。
    """
    if not cwd:
        return False
    normalized = cwd.replace("\\", "/").rstrip("/")
    # /tmp/oc-manager-codex/... 、/tmp/oc-manager-claude/... 、以及嵌套变体
    parts = [p for p in normalized.split("/") if p]
    return any(p.startswith("oc-manager-") for p in parts)


def parse_timestamp(value) -> float | None:
    """解析 ISO8601 时间戳字符串（含尾部 Z）为 epoch 秒；非字符串或格式错误返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# 工具名 → 语义类别。手机端按类别选图标与配色，不认识的一律 other。
# 从 remote.richmsg 下沉：这是各端共用的无状态词表，不属于远程展示层。
_KIND_BY_NAME = {
    "read": "read",
    "read_file": "read",
    "view": "read",
    "edit": "edit",
    "str_replace": "edit",
    "strreplace": "edit",
    "apply_patch": "edit",
    "multiedit": "edit",
    "write": "write",
    "create_file": "write",
    "notebookedit": "edit",
    "bash": "shell",
    "shell": "shell",
    "exec": "shell",
    "exec_command": "shell",
    "run_terminal_cmd": "shell",
    "local_shell": "shell",
    "grep": "search",
    "glob": "search",
    "search": "search",
    "codebase_search": "search",
    "search_code": "search",
    "websearch": "web",
    "web_search": "web",
    "webfetch": "web",
    "fetch": "web",
    "task": "task",
    "todowrite": "todo",
    "todo_write": "todo",
    "askuserquestion": "question",
    "askquestion": "question",
    "request_user_input": "question",
    "question": "question",
}


def classify_tool(name: str) -> str:
    """把工具名映射为语义类别；未收录的返回 other。"""
    return _KIND_BY_NAME.get((name or "").strip().lower(), "other")


def is_cursor_agent_cmdline(cmdline: str) -> bool:
    """判断命令行是否为 Cursor Agent CLI 主进程（不是 worker-server）。

    新版 agent 会把 ``/proc/<pid>/comm`` 改成 ``MainThread``，``pgrep -x agent``
    因此失效；判活必须改认 argv0=``agent`` 或 ``cursor-agent/.../index.js``。
    """
    text = str(cmdline or "").strip()
    if not text or "worker-server" in text:
        return False
    argv0 = text.split(None, 1)[0]
    if os.path.basename(argv0) == "agent":
        return True
    normalized = text.replace("\\", "/")
    return "cursor-agent/" in normalized and "/index.js" in normalized


def is_pi_cmdline(cmdline: str) -> bool:
    """判断命令行是否为 Pi coding agent 主进程。

    npm 全局安装的 ``pi`` 是 ``#!/usr/bin/env node`` 的 ``cli.js``，进程 comm 是
    ``node`` 不是 ``pi``，``pgrep -x pi`` 会漏掉。标题生成用 ``--no-session``，
    不落盘、不能当作用户会话判活。
    """
    text = str(cmdline or "").strip()
    if not text:
        return False
    tokens = text.split()
    if "--no-session" in tokens:
        return False
    argv0 = tokens[0]
    if os.path.basename(argv0) == "pi":
        return True
    normalized = text.replace("\\", "/")
    return "pi-coding-agent/" in normalized and "/cli.js" in normalized


def _pids_matching_cmdline(predicate) -> list[int]:
    """按 cmdline 谓词扫进程 pid；失败返回空列表。"""
    pids: list[int] = []
    if sys.platform.startswith("linux"):
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if not raw:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
            if predicate(cmdline):
                pids.append(int(name))
        return pids
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if predicate(parts[1]):
            pids.append(pid)
    return pids


def _cursor_agent_pids_by_cmdline() -> list[int]:
    """按 cmdline 扫描 Cursor agent 主进程 pid；失败返回空列表。"""
    return _pids_matching_cmdline(is_cursor_agent_cmdline)


def _pi_pids_by_cmdline() -> list[int]:
    """按 cmdline 扫描 Pi 主进程 pid；失败返回空列表。"""
    return _pids_matching_cmdline(is_pi_cmdline)


def _pids_for_process_name(process_name: str) -> list[int]:
    """精确进程名 +（agent / pi）cmdline 兜底，合并去重。"""
    found: list[int] = []
    seen: set[int] = set()
    try:
        raw = subprocess.check_output(
            ["pgrep", "-x", process_name], stderr=subprocess.DEVNULL
        ).decode().split()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        raw = []
    for pid_str in raw:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid not in seen:
            seen.add(pid)
            found.append(pid)
    if process_name == "agent":
        extra = _cursor_agent_pids_by_cmdline()
    elif process_name == "pi":
        extra = _pi_pids_by_cmdline()
    else:
        extra = []
    for pid in extra:
        if pid not in seen:
            seen.add(pid)
            found.append(pid)
    return found


def live_pid_snapshot(process_name: str) -> tuple[int, ...]:
    """廉价进程快照：只做 pgrep / cmdline 兜底，不查 cwd、不调 lsof。

    供各运行时 ``scan_signature`` 判断「有没有进程启停」。cwd / 打开文件仍走
    ``live_processes``，且按 pid 集合缓存，避免后台重扫每 3 秒对每个 pid 再 fork
    一次 ``lsof``（本机 15 个 Cursor agent 曾测到单轮 ~800ms）。
    """
    pids = _pids_for_process_name(process_name)
    _track_live_pids(process_name, pids)
    return tuple(sorted(pids))


def stat_signature(paths) -> tuple[tuple[str, int, int], ...]:
    """文件级元数据签名：``(path, mtime_ns, size)`` 排序元组，缺文件跳过。

    这是多层历史目录上唯一可靠的廉价预检——祖先目录 mtime 不会因既有文件追加
    而冒泡，禁止拿父目录 mtime 当 ``scan_signature``。
    """
    rows: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append((path, st.st_mtime_ns, st.st_size))
    rows.sort()
    return tuple(rows)


_LIVE_CWD_CACHE: dict[str, tuple[tuple[int, ...], list[tuple[int, str]]]] = {}
_PROC_CMDLINE_CACHE: dict[int, str] = {}
_PROC_ENVIRON_CACHE: dict[int, dict[str, str]] = {}
_LIVE_PIDS_BY_NAME: dict[str, frozenset[int]] = {}


def _track_live_pids(process_name: str, pids: list[int]) -> None:
    """记下本轮仍活着的 pid；离开集合的进程丢掉命令行/环境缓存，避免 pid 复用串味。"""
    current = frozenset(pids)
    previous = _LIVE_PIDS_BY_NAME.get(process_name, frozenset())
    for pid in previous - current:
        _PROC_CMDLINE_CACHE.pop(pid, None)
        _PROC_ENVIRON_CACHE.pop(pid, None)
    _LIVE_PIDS_BY_NAME[process_name] = current


def clear_live_cwd_cache() -> None:
    """测试用：清掉按 pid 集缓存的 cwd / 命令行 / 环境，避免用例之间串味。"""
    _LIVE_CWD_CACHE.clear()
    _PROC_CMDLINE_CACHE.clear()
    _PROC_ENVIRON_CACHE.clear()
    _LIVE_PIDS_BY_NAME.clear()


def _cwds_for_pids(pids: list[int]) -> list[tuple[int, str]]:
    """解析一批 pid 的 cwd；macOS 必须一次合并 lsof，禁止逐 pid fork。"""
    if not pids:
        return []
    found: list[tuple[int, str]] = []
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
            found.append((pid, os.path.realpath(cwd)))
        return found
    if sys.platform != "darwin":
        return []
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(pid) for pid in pids)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return []
    current: int | None = None
    by_pid: dict[int, str] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
            continue
        if current is None or not line.startswith("n"):
            continue
        by_pid[current] = os.path.realpath(line[1:])
    for pid in pids:
        cwd = by_pid.get(pid)
        if cwd:
            found.append((pid, cwd))
    return found


def live_processes(process_name: str) -> list[tuple[int, str]]:
    """返回全部存活同名进程的 ``(pid, 归一化 cwd)`` 列表。

    与 `live_pids_by_process_name` 不同，这里**不会**按 cwd 去重——同一工作目录
    下可以同时跑多个 agent（例如跨助手接力新建的 Cursor 与旧的 `--resume`
    会话并存）。调用方若只能保守地标「该目录最新一条」，再自行折叠；若能从
    命令行解析出会话 ID，则应逐进程精确绑定。

    对 ``agent``：除 ``pgrep -x`` 外还会按 cmdline 兜底（Cursor 会把 comm 改成
    ``MainThread``）。对 ``pi``：npm 包装后 comm 常是 ``node``，同样按 cmdline
    兜底。已知局限：同名的其它子命令进程（如 ``<name> serve``）
    会被一并计入，调用方（OpenCode）必须自己排除非 TUI。任一环节失败都静默
    降级为空列表，不抛异常。

    pid 集合没变时复用上一轮 cwd，避免后台重扫把 macOS ``lsof`` 再付一遍。
    """
    pids = _pids_for_process_name(process_name)
    _track_live_pids(process_name, pids)
    key = tuple(sorted(pids))
    cached = _LIVE_CWD_CACHE.get(process_name)
    if cached is not None and cached[0] == key:
        return list(cached[1])
    found = _cwds_for_pids(pids)
    _LIVE_CWD_CACHE[process_name] = (key, found)
    return list(found)


def _etime_seconds(text: str) -> float | None:
    """把 ``ps -o etime=`` 的 ``[[dd-]hh:]mm:ss`` 转成已运行秒数。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        day_part, raw = raw.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    try:
        nums = [int(part) for part in raw.split(":")]
    except ValueError:
        return None
    if len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def process_start_time(pid: int) -> float | None:
    """进程启动的 unix 时间戳；失败返回 None。

    macOS 的 ``ps`` 没有 ``etimes``，统一解析 ``etime``（两端都有）。
    """
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "etime="],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return None
    elapsed = _etime_seconds(out)
    if elapsed is None:
        return None
    return time.time() - elapsed


def live_pids_by_process_name(process_name: str) -> dict[str, int]:
    """返回「工作目录 -> pid」映射，供没有 pid 注册表、也不能靠 lsof 定位单个
    历史文件、且同目录只标最新一条的运行时（目前是 Kimi Code）复用：找到存活的
    同名进程，读取其当前工作目录，与会话记录的工作目录字段匹配。

    OpenCode 已改走 `live_processes` 精确绑定，不要再把这条折叠映射套回去。
    同一 cwd 有多个同名进程时只保留其中一个（遍历顺序下的最后一个）。调用方
    需要自行只把该目录最新一条会话标记存活。需要保留全部进程时改用
    `live_processes`。任一环节失败都静默降级为空集，不抛异常。
    """
    live: dict[str, int] = {}
    for pid, cwd in live_processes(process_name):
        live[cwd] = pid
    return live


def process_command_line(pid: int) -> str:
    """读取进程命令行；失败返回空串。供扫描器从 `--resume <id>` 等参数精确绑会话。

    同一 pid 在仍存活期间复用上一轮结果，避免后台重扫对每个 agent 再 ``ps`` 一次。
    """
    cached = _PROC_CMDLINE_CACHE.get(pid)
    if cached is not None:
        return cached
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                text = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        else:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                stderr=subprocess.DEVNULL,
            )
            text = out.decode(errors="replace").strip()
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        text = ""
    _PROC_CMDLINE_CACHE[pid] = text
    return text


def process_environ(pid: int) -> dict[str, str]:
    """读取进程环境变量；失败返回空字典。

    供扫描器从托管注入的 ``CORRAL_SESSION_ID`` / ``SC_SESSION_ID`` /
    ``PI_CODING_AGENT_SESSION_DIR`` 精确绑会话。
    Linux 读 ``/proc/<pid>/environ``；macOS 用 ``ps eww``（输出混在命令行尾部）。
    同一 pid 在仍存活期间复用上一轮结果。
    """
    cached = _PROC_ENVIRON_CACHE.get(pid)
    if cached is not None:
        return dict(cached)
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
            if not raw:
                _PROC_ENVIRON_CACHE[pid] = {}
                return {}
            env: dict[str, str] = {}
            for item in raw.split(b"\x00"):
                if not item or b"=" not in item:
                    continue
                key, value = item.decode(errors="replace").split("=", 1)
                env[key] = value
            _PROC_ENVIRON_CACHE[pid] = env
            return dict(env)
        out = subprocess.check_output(
            ["ps", "eww", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        _PROC_ENVIRON_CACHE[pid] = {}
        return {}
    env: dict[str, str] = {}
    # ps eww 把环境变量拼在同一行；只提取我们关心的键，避免把命令参数误当环境。
    for key in PROCESS_ENV_KEYS:
        marker = f"{key}="
        start = out.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = start
        while end < len(out) and not out[end].isspace():
            end += 1
        env[key] = out[start:end]
    _PROC_ENVIRON_CACHE[pid] = env
    return dict(env)


def open_file_paths(pids: list[int]) -> dict[int, list[str]]:
    """批量读取进程打开的文件路径；失败的 pid 不出现在结果里。

    Linux 读 ``/proc/<pid>/fd``；其余平台一次 ``lsof -Fn``。
    供 Cursor 等从打开的 ``store.db`` 反推真实会话 ID。
    """
    if not pids:
        return {}
    result: dict[int, list[str]] = {pid: [] for pid in pids}
    if sys.platform.startswith("linux"):
        for pid in pids:
            fd_dir = f"/proc/{pid}/fd"
            try:
                names = os.listdir(fd_dir)
            except OSError:
                result.pop(pid, None)
                continue
            paths: list[str] = []
            for name in names:
                try:
                    paths.append(os.readlink(os.path.join(fd_dir, name)))
                except OSError:
                    continue
            result[pid] = paths
        return {pid: paths for pid, paths in result.items() if paths is not None}

    try:
        out = subprocess.check_output(
            ["lsof", "-Fn", "-p", ",".join(str(pid) for pid in pids)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
            continue
        if current is None or current not in result:
            continue
        if line.startswith("n"):
            result[current].append(line[1:])
    return result
