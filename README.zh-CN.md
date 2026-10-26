# SessKit

**语言：** [English](README.md) | 简体中文

从磁盘解析并导出本机 **Claude Code**、**Codex CLI**、**Cursor** 助手的会话文件，统一成一套 JSON 契约。同时支持 OpenCode、Kimi Code、Pi。

这是 **Python 解析库 + JSON 命令行**，不是终端界面，也不是会话管理器。其他工具可调用 `sesskit` CLI，或按公开 JSON Schema 自行实现。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 用 SessKit 构建工具

- 将本地对话送入自己的搜索索引或知识库。
- 不解析运行时原始文件，直接读取原生用量与历史结局。
- 在自己的会话客户端中消费增量事件。

从[接入指南和三个可运行示例](docs/INTEGRATION_GUIDE.md)开始。
读取已有本地历史不需要模型账号或 API key。

## 安装

SessKit 尚未上架 PyPI。请在项目的虚拟环境中安装固定发布版本的 wheel。

需要 **Python 3.10+**。支持的平台：**macOS 与 Linux**（助手历史落在 `~` 下）。不宣称支持 Windows。

```bash
python -m pip install "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"
python -m sesskit describe
```

macOS 和 Linux 使用相同命令，无需 Git 或本地构建工具。
尚无虚拟环境时，先运行 `python3 -m venv .venv && . .venv/bin/activate`。
只使用 CLI 时，可将同一个 wheel URL 交给 `pipx install`。参与源码开发时，
克隆仓库后运行 `python -m pip install -e .`。

## 快速开始

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <会话id或前缀> --full
sesskit share <会话id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit describe
sesskit verify --sample 30
```

修改解析器或加载链路后，`sesskit verify` 是必跑的真实历史一致性检查。它会对照普通对话、v1 事件、typed 活动记录和受支持的增量读取结果，并输出各运行时的计数、结局、违反项与耗时，不输出内容或路径。扫描、加载、读取或探针失败都不能判为通过；没有样本时会明确标记为未验证，而不是通过；独立读取期间若历史仍在变化，则标记为未定，也不能算通过。Kimi 暂不支持 typed 活动记录和增量读取是预期能力边界，但仍会执行旧版对话与事件检查。这项检查只验证 SessKit 自身各视图是否一致，不是独立的语义正确性判定。检查失败或未验证时，命令会以非零状态结束。

命令均输出同一套 JSON envelope：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

## Python API

```python
from sesskit import load_session_conversation, refresh_session
from sesskit.registry import default_registry
from sesskit.transcript import load_events, SCHEMA_ID

registry = default_registry()
sessions = registry.scan_all(limit=20)
for runtime_id, items in sessions.items():
    for session in items[:3]:
        messages = load_session_conversation(session)
        events = load_events(session)  # SCHEMA_ID == "sesskit.transcript/v1"
        # 两次扫描之间刷新本条会话；历史已不存在时返回 None。
        fresh = refresh_session(session)
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

结构化活动、增量读取、提问、原生轮次标记、用量与压缩记录的支持情况，见
[能力矩阵](docs/INTEGRATION_GUIDE.md#capability-matrix)。`sesskit describe` 也会返回能力声明；
拿不到的数据不等于零，也不能当成成功证据。

只读。不会启动助手、恢复对话，也不会改写历史文件。

会真实启动助手会话的自动化（测试、评测、探针）可以在工作区根目录放一个空的 `.sesskit-ignore` 文件，让这些会话不进列表；见 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 契约

见 [`schemas/`](schemas/) 与 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 选择接入方式

| 需求 | 接口 |
|---|---|
| 普通对话 | Python `load_session_conversation`，或 CLI `show` / `export` |
| 在其他语言中读取兼容 JSON 事件 | CLI `share` + transcript v1 schema |
| 丰富的证据、用量、结局或增量读取 | Python API，见[接入指南](docs/INTEGRATION_GUIDE.md) |

Transcript v1 是兼容投影，不包含更丰富的 Python 模型中的所有字段。
`sesskit search` 搜索标题和摘要，不搜索完整对话正文。
导出内容可能包含秘密；分享或上传前请先检查。

## 非目标（v1）

- 启动或控制助手
- 终端界面或会话管理器
- 写入或删除会话历史
- Go / TypeScript 绑定（请消费 CLI / Schema）

## 你在构建什么？

[反馈你的接入场景](https://github.com/x0c/sesskit/issues/new?template=integration.yml)：
需要读取哪些运行时、希望得到什么输出、遇到了什么阻碍。不要提交凭据或完整私人对话；
最小的合成或脱敏示例就能提供帮助。

## 许可证

MIT
