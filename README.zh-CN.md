# SessKit

**语言：** [English](README.md) | 简体中文

从磁盘解析并导出本机 **Claude Code**、**Codex CLI**、**Cursor** 助手的会话文件，统一成一套 JSON 契约。同时支持 OpenCode、Kimi Code、Pi。

这是 **Python 解析库 + JSON 命令行**，不是终端界面，也不是会话管理器。其他工具可调用 `sesskit` CLI，或按公开 JSON Schema 自行实现。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 安装

SessKit 尚未上架 PyPI。请从 GitHub 安装（推荐），或本地源码安装。

需要 **Python 3.10+**。支持的平台：**macOS 与 Linux**（助手历史落在 `~` 下）。不宣称支持 Windows。

```bash
pip install "sesskit @ git+https://github.com/x0c/sesskit.git"
# 或
pipx install "sesskit @ git+https://github.com/x0c/sesskit.git"
# 或从源码
pip install -e .
```

## 快速开始

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <会话id或前缀> --full
sesskit share <会话id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit describe
```

命令均输出同一套 JSON envelope：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

## Python API

```python
from sesskit import load_session_conversation
from sesskit.registry import default_registry
from sesskit.transcript import load_events, SCHEMA_ID

registry = default_registry()
sessions = registry.scan_all(limit=20)
for runtime_id, items in sessions.items():
    for session in items[:3]:
        messages = load_session_conversation(session)
        events = load_events(session)  # SCHEMA_ID == "sesskit.transcript/v1"
```

## 读哪些本地文件

| 运行时 | 作用 |
|---|---|
| Claude Code | 解析 / 导出本机会话文件 |
| Codex CLI | 解析 / 导出本机会话文件 |
| Cursor Agent | 解析 / 导出本机会话文件 |
| OpenCode | 解析 / 导出本机会话文件 |
| Kimi Code | 解析 / 导出本机会话文件 |
| Pi | 解析 / 导出本机会话文件 |

只读。不会启动助手、恢复对话，也不会改写历史文件。

## 契约

见 [`schemas/`](schemas/) 与 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 和同类工具比

| | SessKit | 常见 dump / 用量看板 |
|---|---|---|
| 形态 | 解析库 + JSON 命令行 | 多为终端界面、网页或一次性脚本 |
| 契约 | JSON Schema + 稳定 envelope | 通常只有 Python/CLI |
| 记录 | 纯文本（`export` / `show`）与富事件（`share`） | 各不相同 |
| 运行时 | Claude Code、Codex、Cursor、OpenCode、Kimi、Pi | 常见 1–3 个 |

## 非目标（v1）

- 启动或控制助手
- 终端界面或会话管理器
- 写入或删除会话历史
- Go / TypeScript 绑定（请消费 CLI / Schema）

## 许可证

MIT
