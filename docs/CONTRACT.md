# SessKit contract (v1)

## Envelope

Every CLI command prints one JSON object to stdout:

```json
{"ok": true, "data": {}, "error": null, "meta": {"version": 1}}
```

On failure `ok` is false, `data` is null, and `error` has `code`, `message`, `hint`, `next_commands`.

Exit codes: `0` ok, `1` error, `2` usage, `3` not found, `5` ambiguous.

## Schemas

| File | Purpose |
|---|---|
| `schemas/session.v1.json` | List/search session object |
| `schemas/conversation.v1.json` | Plain-text message in show/export |
| `schemas/transcript.v1.json` | Rich event in share (`schema` field = `sesskit.transcript/v1`) |

Legacy Corral share payloads used `corral.share/v1`; readers should accept both ids.

## Supported runtimes

`claude`, `codex`, `opencode`, `kimi`, `cursor`, `pi`

## Language-agnostic access

1. Shell out to `sesskit` and parse the envelope.
2. Or reimplement parsers against the JSON Schema; Python under `src/sesskit` is the reference implementation.
