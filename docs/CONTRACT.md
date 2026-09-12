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
| `schemas/session.v1.json` (packaged as `sesskit/schemas/…`) | List/search session object |
| `schemas/conversation.v1.json` | Plain-text message in show/export |
| `schemas/transcript.v1.json` | Rich event in share (`schema` field = `sesskit.transcript/v1`) |

Root `schemas/` is the doc-facing copy; wheels also ship identical files under `src/sesskit/schemas/`. When changing a schema, update **both** locations in the same change.

Legacy Corral share payloads used `corral.share/v1`; readers should accept both ids.

## Public conversation load

- **Session-dict entry (CLI + library):** `sesskit.load_session_conversation(session)` / `RuntimeParser.load_conversation(session)`.
- **Parser modules stay path-based** (OpenCode: `db_path` + `session_id`). Do not pass a whole session dict into `parsers.*.load_conversation`.
- **Missing history is an error** for the SessKit CLI (`ConversationLoadError` → envelope `ok: false`). Do not disguise “file gone / unreadable” as a successful empty transcript.
- **Corral soft-fails** on the same adapter (`sesskit_bridge.load_runtime_conversation` returns `[]`) so the TUI never crashes on one bad file. Do not “unify” that into raising inside Corral.

## Listing modes

- List `first_user_msg` / `last_user_msg` / `last_agent_msg` stay at most 300 characters. When the text is a Corral handoff wrapper, extract the inherited `Task:` line and the conversation digest **before** clipping; otherwise the 300-char window is consumed by pickup boilerplate and title generation never sees the real request. Nested pickups flatten an earlier wrapper into `[Original request]` / `【原始需求】` on one line — peel inward (inner task wins) and drop leftover `You are picking up` on that line. Do not raise the raw slice to recover those bytes.
- Default scan drops sessions whose project `cwd` no longer exists (resume-oriented; **Corral must keep this default**).
- Pass `--include-missing-cwd` (or `include_missing_cwd=True` on scanners) for archive/search when history files still exist.
- Never turn `include_missing_cwd` on inside Corral’s recover / sidebar path.

## Export / share writes

- Refuse writing onto a session history path (or its symlink); see `sesskit.paths.assert_not_history_path`.
- Write JSON via same-directory temp file + `os.replace` (`atomic_write_json`). Do not truncate the destination in place.

## Runtime-specific restore semantics

- **Pi:** when reconstructing the active branch from a v2+ tree, start from the **last appended file entry**, then walk `parentId` (matches pi-mono `buildSessionPath`). Do not pick the leaf by max wall-clock timestamp — clock skew selects the wrong branch.
- **Cursor:** list-level `scan_signature` must **not** include `store.db-wal` (streaming writes would invalidate the whole list every few seconds). Per-session conversation / `extra_version` **must** still include the WAL so previews see uncheckpointed tails. Do not collapse these two version keys into one.

## Supported runtimes

`claude`, `codex`, `opencode`, `kimi`, `cursor`, `pi`

## Language-agnostic access

1. Shell out to `sesskit` and parse the envelope.
2. Or reimplement parsers against the JSON Schema; Python under `src/sesskit` is the reference implementation.

## Install

Not on PyPI yet. Documented install is GitHub (`pip install "sesskit @ git+https://github.com/x0c/sesskit.git"`) or editable source. Corral Homebrew vendors the GitHub Release **sdist** (`scripts/sesskit_dep.py` / formula resource) — do not switch Corral’s runtime dep to a git URL.

## Verification (mandatory when changing parsers or load wiring)

Unit tests that **mock** `load_conversation` can stay green while the real registry wiring is broken. After any change to parsers, registry adapters, transcript parsing, or CLI show/export/share:

1. Run the fixture wiring tests (`tests/test_registry_load.py`) — they must exercise the **real** adapter, not a session-dict stub that hides path wiring.
2. On a machine with local history, sample **every installed runtime** that has sessions (not one Codex-only check):
   - `load_session_conversation` / registry load succeeds (no `TypeError` from passing a dict as a path).
   - Roles are only `user` / `assistant`; no empty text; no literal `"None"`.
   - Strong system / injection markers must not appear in plain user turns (e.g. `system reminder`, `<system`, `task-notification`, `<ide_opened_file`, `<agent_skills>`, `<mcp_file_system>`, `<function_calls>`).
   - Every plain user text appears in `load_events` `user_message` texts (allow substring match when share splits more finely).
3. If Corral consumes the same change: confirm Corral’s `sesskit_bridge` path and SessKit registry path return the **same** `(role, text)` sequences for the sampled sessions.
4. Do not claim “data correctness OK” from fixture-only runs when real history is available.
