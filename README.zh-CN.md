# SessKit

**语言：** [English](README.md) | 简体中文

从磁盘解析并导出本机 **Claude Code**、**Codex CLI**、**Cursor**、**OpenCode**、**Kimi Code**、**Pi** 和 **Antigravity** 助手的会话文件，统一成一套 JSON 契约。

这是 **Python 解析库 + JSON 命令行**，不是终端界面，也不是会话管理器。其他工具可调用 `sesskit` CLI，或按公开 JSON Schema 自行实现。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 用 SessKit 构建工具

- 将本地对话送入自己的搜索索引或知识库。
- 不解析运行时原始文件，直接读取各助手的用量数据与历史记录。
- 在自己的会话客户端中消费增量事件。

从[接入指南和三个可运行示例](docs/INTEGRATION_GUIDE.md)开始。
读取本地已有历史不需要模型账号或 API key。

## 消息流

三方程序可通过 `open_message_stream(session)` 或 `sesskit stream` 持续显示消息更新。
宿主提供原生事件流时可逐段显示；实时来源不可用时会自动回退到已落盘历史。
[消息流接入指南](docs/MESSAGE_STREAMING_GUIDE.md)说明了 Python 接入、JSONL 协议及观察预算。
v0.2.25 及后续版本已包含此能力。

## 安装

SessKit 尚未上架 PyPI。请在项目的虚拟环境中安装指定版本的 wheel。

需要 **Python 3.10+**。支持的平台：**macOS 与 Linux**（助手历史落在 `~` 下）。暂不支持 Windows。

```bash
python -m pip install --no-index --find-links "https://github.com/x0c/sesskit/releases/expanded_assets/v0.3.2" sesskit
python -m sesskit describe
```

已有 pip 管理的安装可用 `sesskit upgrade --dry-run` 只检查最新版本，用
`sesskit upgrade` 执行只升不降的升级并回读版本。无需调用 `gh`、手工查 tag、
运行 `pip show` 或转换 `v` 前缀。由其他包管理器拥有（例如 Homebrew）的安装
仍使用该管理器的升级路径。

macOS 和 Linux 使用相同命令，无需 Git 或本地构建工具。
尚无虚拟环境时，先运行 `python3 -m venv .venv && . .venv/bin/activate`。
安装器会优先选择适配 macOS/Linux、Apple Silicon、Intel/AMD 或 ARM64 的
Rust 加速 wheel；没有适配产物时使用纯 Python 兼容 wheel。现有 Python API
与 JSON 输出保持不变。设置 `SESSKIT_NATIVE=0` 可使用 Python 路径。
源码构建需要 Rust 1.88+，安装预编译 wheel 则不需要。
参与源码开发时，克隆仓库后运行 `python -m pip install -e .`。

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

修改解析器或加载链路后，请运行 `sesskit verify`：它会交叉核对普通对话、v1 事件、类型化活动记录和增量读取，并报告各运行时的计数、结果、违反项和耗时——不输出内容或路径。检查失败时会以非零状态退出。

快照命令均输出同一套 JSON 信封：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1, "tool_version": "0.3.2"}}
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
| Antigravity | 解析 / 导出本机会话文件 |

结构化活动、增量读取、提问、原生轮次标记、用量与压缩记录的支持情况，见
[能力矩阵](docs/INTEGRATION_GUIDE.md#capability-matrix)。`sesskit describe` 也会返回能力声明；
拿不到的数据不等于零，也不能当成成功证据。

会话历史保持只读。不会启动助手、恢复对话或改写历史文件；显式运行的
`upgrade` 命令只更改 SessKit 自己由 pip 管理的安装环境。

会真实启动助手会话的自动化（测试、评测、探针）可以在工作区根目录放一个空的 `.sesskit-ignore` 文件，即可将其排除在列表之外；见 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 原生指标

发布版提供去重对账后的 Token／费用数据，以及历史耗时和生成速度。

```bash
sesskit usage pi:SESSION_ID
sesskit usage pi:SESSION_ID --message-id MESSAGE_ID
sesskit performance pi:SESSION_ID
sesskit performance pi:SESSION_ID --detail records --limit 100
```

Python 接口：`load_usage`、`summarize_usage`、`UsageSelection`、`load_performance`。
只需一个 Token 数时读取 `data.summary.metrics.total_tokens.total_to_report`，
并同时保留旁边的 `coverage`；部分数据不等于完整总量。保留每次调用、消息、
轮次和会话的统计范围与原生计时含义；缺失不是零，客户端耗时也不是服务端
纯解码速度。SessKit 提供数据，不做评测打分。
运行时支持、筛选、示例和 Schema 见[接入指南](docs/INTEGRATION_GUIDE.md#historical-timing-and-generation-speed)。

## 契约

见 [`schemas/`](schemas/) 与 [`docs/CONTRACT.md`](docs/CONTRACT.md)。

## 选择接入方式

| 需求 | 接口 |
|---|---|
| 普通对话 | Python `load_session_conversation`，或 CLI `show` / `export` |
| 在其他语言中读取兼容 JSON 事件 | CLI `share` + transcript v1 schema |
| 丰富的证据、用量、结果或增量读取 | Python API，见[接入指南](docs/INTEGRATION_GUIDE.md) |

Transcript v1 是面向兼容的精简视图，不包含更丰富的 Python 模型中的所有字段。
`sesskit search` 搜索标题和摘要，不搜索完整对话正文。
导出内容可能包含密钥等敏感信息；分享或上传前请先检查。

## 非目标（v1）

- 启动或控制助手
- 终端界面或会话管理器
- 写入或删除会话历史
- Go / TypeScript 绑定（请直接使用 CLI / Schema）

## 你在构建什么？

[反馈你的接入场景](https://github.com/x0c/sesskit/issues/new?template=integration.yml)：
需要读取哪些运行时、希望得到什么输出、遇到了什么阻碍。不要提交凭据或完整私人对话；
最小的合成或脱敏示例就能提供帮助。

## 发布校验（维护者）

安装用户看到[安装](#安装)即可：SessKit 尚未上架 PyPI。

维护者无需信任工作树即可校验已发布的版本：

```bash
python scripts/publish.py verify --version 0.2.23 --check-only
```

`v0.2.23` 产物的预期哈希（GitHub release API 中也有记录）：

- wheel `sesskit-0.2.23-py3-none-any.whl`：`sha256:30fcf7e063c29d594262a8223b368af2d0e6bd8958e3b65792ada2d9a8964d81`
- sdist `sesskit-0.2.23.tar.gz`：`sha256:acc70a9033f0f9c945ad4f0df53600f61ff5ea5417580df5c930410c2d76f084`

本地发布不依赖 CI。先校验，再用环境变量里的凭据上传——该脚本没有
password/token 参数，密钥不会出现在 argv 或 shell 历史上。先把 PyPI
token 存进系统密钥库一次（运行时提示输入，命令行里不出现密钥明文），
上传时再引用：

```bash
python scripts/publish.py check-dist --version 0.2.23 --dist dist
security add-generic-password -s pypi-sesskit -a __token__ -w
TWINE_USERNAME=__token__ TWINE_PASSWORD="$(security find-generic-password -s pypi-sesskit -w)" \
  python scripts/publish.py upload --version 0.2.23 --dist dist --confirm-upload
```

（Linux 请用各自的密钥库，例如 `secret-tool lookup server pypi.org`，
保持同一模式：命令里不写密钥明文。）

CI 发布由 [.github/workflows/publish-pypi.yml](.github/workflows/publish-pypi.yml)
在 `v*` tag 推送（或手动 dispatch 已有 tag）时经 PyPI Trusted Publishing
完成——无预存 API 令牌（GitHub 每次运行按需签发短期 OIDC 凭据）。

## 🧩 AI 编程工具链里的相关工具

如果你每天在多个 AI 编程助手之间切换，看看这套本地优先工具链的其余部分：

| 仓库 | 说明 |
| :--- | :--- |
| **[corral](https://github.com/corral-dev/corral)** | 在终端会话管理器里并排跑 Claude Code、Codex、OpenCode、Cursor 和 Pi，再从 iPhone 或 Mac 上继续。 |
| **[subswap](https://github.com/x0c/subswap)** | 查剩余额度，一键切换 Claude Code、Codex、Cursor、Kimi Code 和 OpenCode 的账号，不用反复登录。 |
| **[agentsync](https://github.com/x0c/agentsync)** | 以一份本地源统一同步 `AGENTS.md`、`CLAUDE.md`、skills 和 MCP 配置到各编程助手。 |
| **[agent-doc-skills](https://github.com/x0c/agent-doc-skills)** | 可导航的项目知识库 skills（`doc-init`、`doc-update`、`doc-compact`），让 coding agent 不再每个会话都重新认识你的仓库。 |
| **[agent-session-control](https://github.com/x0c/agent-session-control)** | 用 Python 启动与跟踪本地编码助手会话（库 + JSON CLI + ACP 适配器）；历史解析委托给 SessKit。 |

## 许可证

MIT
