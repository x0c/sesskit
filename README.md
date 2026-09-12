# SessKit

**Languages:** English | [简体中文](README.zh-CN.md)

Parse and export local **Claude Code**, **Codex CLI**, and **Cursor** agent session files from disk into one JSON schema. Also reads OpenCode, Kimi Code, and Pi.

This is a **Python library and JSON CLI** — not a TUI, not a session manager. Other tools integrate by shelling out to `sesskit` or by validating against the published JSON Schema.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Install

SessKit is not on PyPI yet. Install from GitHub (recommended) or from a local checkout.

Requires **Python 3.10+**. Supported platforms: **macOS and Linux** (agents store history under `~`). Not claimed on Windows.

```bash
pip install "sesskit @ git+https://github.com/x0c/sesskit.git"
# or
pipx install "sesskit @ git+https://github.com/x0c/sesskit.git"
# or from source
pip install -e .
```

## Quick start

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <session-id-or-prefix> --full
sesskit share <session-id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit describe
```

Every command prints one JSON envelope:

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

## What it reads

| Runtime | Role |
|---|---|
| Claude Code | Parse / export local session files |
| Codex CLI | Parse / export local session files |
| Cursor Agent | Parse / export local session files |
| OpenCode | Parse / export local session files |
| Kimi Code | Parse / export local session files |
| Pi | Parse / export local session files |

Read-only. It does not launch agents, resume chats, or write history.

## Schemas

See [`schemas/`](schemas/) and [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Compared to similar tools

| | SessKit | Typical dump / usage dashboards |
|---|---|---|
| Shape | Library + JSON CLI | Often a TUI, web UI, or one-off script |
| Contract | JSON Schema + stable envelope | Usually Python/CLI only |
| Transcript | Plain messages (`export` / `show`) and rich events (`share`) | Varies |
| Runtimes | Claude Code, Codex, Cursor, OpenCode, Kimi, Pi | Often 1–3 |

## Non-goals (v1)

- Launching or controlling agents
- A terminal UI or session manager
- Writing or deleting session history
- Go/TypeScript bindings (consume the CLI/schema instead)

## License

MIT
