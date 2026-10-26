# SessKit

**Languages:** English | [简体中文](README.zh-CN.md)

Parse and export local **Claude Code**, **Codex CLI**, and **Cursor** agent session files from disk into one JSON schema. Also reads OpenCode, Kimi Code, and Pi.

This is a **Python library and JSON CLI** — not a TUI, not a session manager. Other tools integrate by shelling out to `sesskit` or by validating against the published JSON Schema.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Build with SessKit

- Feed local conversations into your own search index or knowledge base.
- Inspect native usage and historical outcomes without parsing runtime files.
- Follow incremental events in your own session client.

Start with the [integration guide and three runnable examples](docs/INTEGRATION_GUIDE.md).
No model account or API key is needed to read existing local history.

## Install

SessKit is not on PyPI yet. Install the fixed-release wheel in your project's virtual environment.

Requires **Python 3.10+**. Supported platforms: **macOS and Linux** (agents store history under `~`). Not claimed on Windows.

```bash
python -m pip install "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"
python -m sesskit describe
```

The same command works on macOS and Linux; no Git or local build tools are needed.
Need a virtual environment first? Run `python3 -m venv .venv && . .venv/bin/activate`.
For a standalone CLI, use `pipx install` with the same wheel URL. For source
contributions, clone the repository and run `python -m pip install -e .`.

## Quick start

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <session-id-or-prefix> --full
sesskit share <session-id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
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

Every command prints one JSON envelope:

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
        # Refresh this session between scans; None if its history is gone.
        fresh = refresh_session(session)
```

## What it reads

| Runtime | Role |
|---|---|
| Claude Code | Parse / export local session files |
| Codex CLI | Parse / export local session files |
| Cursor Agent | Parse / export local session files |
| OpenCode | Parse / export local session files |
| Kimi Code | Parse / export local session files |
| Pi | Parse / export local session files |

See the [capability matrix](docs/INTEGRATION_GUIDE.md#capability-matrix) for typed
activity, incremental reading, questions, native turn markers, usage, and compaction.
Capabilities are also returned by `sesskit describe`; unavailable data is not zero
or proof of success.

Read-only. It does not launch agents, resume chats, or write history.

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

## Non-goals (v1)

- Launching or controlling agents
- A terminal UI or session manager
- Writing or deleting session history
- Go/TypeScript bindings (consume the CLI/schema instead)

## What are you building?

[Share your integration use case](https://github.com/x0c/sesskit/issues/new?template=integration.yml):
which runtimes you read, what output you need, and what blocked you. Do not post
credentials or complete private transcripts; synthetic or redacted examples help.

## License

MIT
