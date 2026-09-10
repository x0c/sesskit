# SessKit

**Languages:** English | [简体中文](README.zh-CN.md)

Read, parse, and export local coding-agent sessions into one JSON schema — Claude Code, Codex CLI, OpenCode, Kimi Code, Cursor Agent, and Pi.

Any tool can integrate by shelling out to the `sesskit` CLI (stable JSON envelope) or by validating against the published JSON Schema. The Python package is the reference implementation.

## Install

```bash
pip install sesskit
# or
pipx install sesskit
# or from source
pip install -e .
```

Requires Python 3.10+. Primary support: **macOS and Linux** (where these agents store history under `~`).

## Quick start

```bash
sesskit list --top 5 --compact
sesskit search refactor --top 3
sesskit show <session-id-or-prefix> --full
sesskit share <session-id> --out /tmp/share.json
sesskit export --since 7d --out /tmp/week.json
sesskit describe
```

All commands print:

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

## Python API

```python
from sesskit.registry import default_registry
from sesskit.transcript import load_events, SCHEMA_ID

registry = default_registry()
sessions = registry.scan_all(limit=20)
for runtime_id, items in sessions.items():
    for session in items[:3]:
        messages = registry.get(runtime_id).load_conversation(session)
        events = load_events(session)  # SCHEMA_ID == "sesskit.transcript/v1"
```

## Schemas

See [`schemas/`](schemas/) and [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Compared to similar tools

| | SessKit | agent-dump / harness-recall |
|---|---|---|
| Cross-language contract | JSON Schema + CLI envelope | Usually Python/CLI only |
| Rich transcript | thinking + tool calls (`share`) | varies |
| Runtimes | 6 (incl. Kimi + Pi + OpenCode SQLite) | often 2–3 |

## Non-goals (v1)

- Launching or controlling agents
- Writing/deleting session history
- Go/TypeScript bindings (consume CLI/schema instead)

## License

MIT
