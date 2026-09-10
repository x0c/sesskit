# SessKit

**语言：** [English](README.md) | 简体中文

读取、解析并导出本机编程助手会话，统一成一套 JSON 契约——支持 Claude Code、Codex CLI、OpenCode、Kimi Code、Cursor Agent、Pi。

任意工具可通过调用 `sesskit` CLI（稳定 JSON envelope）接入，或按公开 JSON Schema 自行实现；Python 包是参考实现。

## 安装

```bash
pip install sesskit
# 或
pipx install sesskit
# 或从源码
pip install -e .
```

需要 Python 3.10+。首发支持：**macOS 与 Linux**（助手历史落在 `~` 下）。

## 快速开始

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <会话id或前缀> --full
sesskit share <会话id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit describe
```

命令均输出：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

## Python API

```python
from sesskit.registry import default_registry
from sesskit.transcript import load_events, SCHEMA_ID

registry = default_registry()
sessions = registry.scan_all(limit=20)
```

## 契约

见 [`schemas/`](schemas/) 与 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 许可证

MIT
