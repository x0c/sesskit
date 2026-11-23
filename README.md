# SessKit

**Languages:** English | [简体中文](README.zh-CN.md)

Parse and export local **Claude Code**, **Codex CLI**, **Cursor**, **OpenCode**, **Kimi Code**, **Pi**, and **Antigravity** agent session files from disk into one JSON schema.

This is a **Python library and JSON CLI** — not a TUI, not a session manager. Other tools integrate by shelling out to `sesskit` or by validating against the published JSON Schema.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Build with SessKit

- Feed local conversations into your own search index or knowledge base.
- Inspect native usage and historical outcomes without parsing runtime files.
- Follow incremental events in your own session client.

Start with the [integration guide and three runnable examples](docs/INTEGRATION_GUIDE.md).
No model account or API key is needed to read existing local history.

## Message streaming

Third-party programs can use `open_message_stream(session)` or `sesskit stream`
to display message updates. Host-provided native feeds deliver partial text;
unavailable feeds automatically fall back to persisted history. See the
[message streaming guide](docs/MESSAGE_STREAMING_GUIDE.md) for the Python bridge,
JSONL protocol and observation budgets. Available in v0.2.25 and later.

## Install

SessKit is not on PyPI yet. Install the fixed-release wheel in your project's virtual environment.

Requires **Python 3.10+**. Supported platforms: **macOS and Linux** (agents store history under `~`). Not claimed on Windows.

```bash
python -m pip install --no-index --find-links "https://github.com/x0c/sesskit/releases/expanded_assets/v0.3.12" sesskit
python -m sesskit describe
```

For an existing pip-managed installation, `sesskit upgrade --dry-run` checks the
latest release without changing anything, and `sesskit upgrade` performs a
forward-only upgrade and verifies the installed version. No `gh` call, manual tag
discovery, `pip show`, or `v`-prefix conversion is needed. Package-manager-owned
installations (for example Homebrew) keep their owner's upgrade route.

The same command works on macOS and Linux; no Git or local build tools are needed.
Need a virtual environment first? Run `python3 -m venv .venv && . .venv/bin/activate`.
The installer selects a prebuilt Rust-accelerated wheel for macOS/Linux on
Apple Silicon, Intel/AMD or ARM64 where available, or a pure-Python compatibility
wheel. Existing Python APIs and JSON output are unchanged. Set `SESSKIT_NATIVE=0`
to use the Python path. Source builds require Rust 1.88+; wheel installs do not.
For source contributions, clone the repository and run `python -m pip install -e .`.

## Quick start

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <session-id-or-prefix> --full
sesskit share <session-id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit skills --limit 20
sesskit describe
sesskit verify --sample 30
```

After changing parsers or load wiring, `sesskit verify` is the required
real-history consistency gate. It checks conversation, v1 events, typed
activity, and supported incremental readers against one another, and reports
per-runtime counts, outcomes, violations, and timings — never content or
paths. Scan/load/reader/probe failures prevent a pass; a runtime with no
samples is marked unverified, not passed. Kimi's documented lack of typed
activity and incremental readers is expected, while its legacy checks still
run. Reads overlapping a live source change are inconclusive, not passes.
This verifies internal consistency, not semantic correctness against an
independent oracle. The command exits non-zero when verification fails or is
unverified.

Snapshot commands print one JSON envelope:

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1, "tool_version": "0.3.12"}}
```

## Python API

```python
from sesskit import load_session_conversation, load_skills, refresh_session
from sesskit.registry import default_registry
from sesskit.transcript import load_events, SCHEMA_ID

registry = default_registry()
sessions = registry.scan_all(limit=20)
for runtime_id, items in sessions.items():
    for session in items[:3]:
        messages = load_session_conversation(session)
        events = load_events(session)  # SCHEMA_ID == "sesskit.transcript/v1"
        skills = load_skills(session)  # SCHEMA_ID == "sesskit.skills/v1" (explicit vs auto)
        # Refresh this session between scans; None if its history is gone.
        fresh = refresh_session(session)
```

## Native metrics

The release includes reconciled token/cost data and historical timing/rates.

```bash
sesskit usage pi:SESSION_ID
sesskit usage pi:SESSION_ID --message-id MESSAGE_ID
sesskit performance pi:SESSION_ID
sesskit performance pi:SESSION_ID --detail records --limit 100
```

Python: `load_usage`, `summarize_usage`, `UsageSelection`, and `load_performance`.
For one token number, use `data.summary.metrics.total_tokens.total_to_report`
and keep its adjacent `coverage`; partial data is not a complete total. Keep
per-call/message/turn/session scope and native timing meanings; missing values
are not zero and client timing is not provider decode speed.
SessKit supplies data, not evaluation scores. See the [integration guide](docs/INTEGRATION_GUIDE.md#historical-timing-and-generation-speed)
for runtime support, selectors, examples and schemas.

## What it reads

| Runtime | Role |
|---|---|
| Claude Code | Parse / export local session files |
| Codex CLI | Parse / export local session files |
| Cursor Agent | Parse / export local session files |
| OpenCode | Parse / export local session files |
| Kimi Code | Parse / export local session files |
| Pi | Parse / export local session files |
| Antigravity | Parse / export local session files |

See the [capability matrix](docs/INTEGRATION_GUIDE.md#capability-matrix) for typed
activity, incremental reading, questions, native turn markers, usage, and compaction.
Capabilities are also returned by `sesskit describe`; unavailable data is not zero
or proof of success.

Session history is read-only. SessKit does not launch agents, resume chats, or
write history. The explicit `upgrade` command changes only its own pip-managed
package installation.

Automation that launches real agent sessions (tests, evals, probes) can keep them out of listings by placing an empty `.sesskit-ignore` file in its workspace root; see [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Schemas

See [`schemas/`](schemas/) and [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Choose an interface

| Need | Interface |
|---|---|
| Plain conversation | Python `load_session_conversation`, or CLI `show` / `export` |
| Compatible JSON events from another language | CLI `share` + transcript v1 schema |
| Rich evidence, usage, outcomes, or incremental readers | Python API; see the [integration guide](docs/INTEGRATION_GUIDE.md) |

Transcript v1 is a compatibility projection, not every field in the richer Python
model. `sesskit search` matches titles and excerpts, not full conversation text.
Exported content may contain secrets; review it before sharing or uploading.

## FAQ

### How do I export Claude Code or Codex session transcripts to JSON?

Run `sesskit export --since 7d --out /tmp/week.json` for a range export, or `sesskit show <session-id-or-prefix> --full` / `sesskit share <session-id> --out /tmp/share.json` for a single session. All snapshot commands output one stable JSON envelope or validate against the published [`schemas/`](schemas/).

### Does SessKit launch agents or modify my local history files?

No. Session history reading is strictly read-only. SessKit never launches agents, resumes chats, or mutates runtime history files (the explicit `sesskit upgrade` command only updates its own pip-managed installation).

## 🧩 Related Tools in the AI Coding Suite

If you run multiple AI coding agents daily, check out the rest of the local-first toolkit:

| Repository | Description |
| :--- | :--- |
| **[corral](https://github.com/corral-dev/corral)** | Run Claude Code, Codex, OpenCode, Cursor & Pi side-by-side in a terminal session manager, then continue from iPhone or Mac. |
| **[subswap](https://github.com/x0c/subswap)** | Check remaining quota and switch Claude Code, Codex, Cursor, Kimi Code & OpenCode accounts without logging out. |
| **[agentsync](https://github.com/x0c/agentsync)** | Sync `AGENTS.md`, `CLAUDE.md`, skills, and MCP configs across coding agents from one local source of truth. |
| **[agent-doc-skills](https://github.com/x0c/agent-doc-skills)** | Navigable project knowledge base skills (`doc-init`, `doc-update`, `doc-compact`) so coding agents don't rediscover your repo every session. |
| **[agent-session-control](https://github.com/x0c/agent-session-control)** | Launch and track local coding-agent sessions from Python (library + JSON CLI + ACP adapter); history observation is delegated to SessKit. |

## Release verification (maintainers)

Maintainers: see [docs/MAINTENANCE.md](docs/MAINTENANCE.md).

## Non-goals (v1)

- Launching or controlling agents — use [agent-session-control](https://github.com/x0c/agent-session-control) instead
- A terminal UI or session manager
- Writing or deleting session history
- Go/TypeScript bindings (consume the CLI/schema instead)

## What are you building?

[Share your integration use case](https://github.com/x0c/sesskit/issues/new?template=integration.yml):
which runtimes you read, what output you need, and what blocked you. Do not post
credentials or complete private transcripts; synthetic or redacted examples help.

## License

MIT
