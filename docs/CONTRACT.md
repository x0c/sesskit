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

- **Codex liveness:** use the shared process snapshot before inspecting open rollout files. On macOS, an executable with a long path can be missed by `pgrep -x codex` while its PID and open history file are present. A missing pgrep match alone is not evidence that the task ended.
- **Pi:** when reconstructing the active branch from a v2+ tree, start from the **last appended file entry**, then walk `parentId` (matches pi-mono `buildSessionPath`). Do not pick the leaf by max wall-clock timestamp — clock skew selects the wrong branch.
- **Cursor:** list-level `scan_signature` must **not** include `store.db-wal` (streaming writes would invalidate the whole list every few seconds). Per-session conversation / `extra_version` **must** still include the WAL so previews see uncheckpointed tails. Do not collapse these two version keys into one.

## Supported runtimes

`claude`, `codex`, `opencode`, `kimi`, `cursor`, `pi`

## Language-agnostic access

1. Shell out to `sesskit` and parse the envelope.
2. Or reimplement parsers against the JSON Schema; Python under `src/sesskit` is the reference implementation.

## Install

Not on PyPI yet. Documented install is GitHub (`pip install "sesskit @ git+https://github.com/x0c/sesskit.git"`) or editable source. Corral Homebrew vendors the GitHub Release **sdist** (`scripts/sesskit_dep.py` / formula resource) — do not switch Corral’s runtime dep to a git URL.

## Status tags and abnormal endings

List `status_tag` values come from `sesskit.titles` (`STATUS_DONE` / `STATUS_PENDING` / `STATUS_ABORTED` / `STATUS_NONE`). They are **history-tail inferences**, not liveness and not a substitute for Corral’s working/waiting/idle phase.

### Required semantics (product rule)

Consumers (Corral notifications, mobile “job finished”, scripts) must be able to tell **successful completion** apart from **abnormal ends**. Abnormal ends that exist in native history **must** be visible through SessKit — at minimum via correct `status_tag`, and ideally via retained detail (error text / code) in list fields and/or `load_events`.

| Outcome | Expected `status_tag` | Detail consumers need |
|---|---|---|
| Normal turn finished with assistant reply | `STATUS_DONE` | `last_agent_msg` / trailing `assistant_message` in `load_events` |
| Waiting on user | `STATUS_PENDING` | last turn is user-owned |
| Rate limit, quota, provider error, abort, interrupt | `STATUS_ABORTED` (or a future dedicated failure tag) | Human-readable error summary must not be dropped |
| Truly unknown | `STATUS_NONE` | Prefer unknown over false `DONE` |

**Do not** treat a native “task/turn complete” marker as success when the same record carries a non-empty error. **Do not** drop error-only assistant turns from `load_events` so that only the preceding user line remains.

### Native signals (reference)

Use these when implementing or reviewing parsers (not an event-bus API — SessKit stays pull/snapshot):

| Runtime | Success signal | Abnormal signal (examples) |
|---|---|---|
| Codex | `event_msg` / `task_complete` **without** `error` | Same event with `error` (e.g. `codex_error_info: usage_limit_exceeded`); `turn_aborted` |
| Pi | assistant `stopReason` in `{stop, …}` with content | `stopReason` in `{error, aborted, …}` plus `errorMessage` (e.g. weekly 429) |
| OpenCode | assistant `finish=stop`, no `error` | Non-empty message `error` → aborted (existing parser rule) |
| Cursor | Prefer transcript / store tail; list `status_tag` is weak today | Prefer unknown over false done when terminal reason is unclear |
| Claude | Stop / transcript end without interrupt markers | `system` entries with `error` (`formatted` human text + `status` HTTP code; 2.1+ live shape) → aborted; `[Request interrupted by user]` → aborted |
| Kimi | Stable `step.end` / assistant tail | Cancel / failure markers in wire history |

### Verified gaps closed (2026-09-15 live Mac runs)

Previously confirmed failures, now covered by parser fixes + `tests/test_abnormal_endings.py` and re-checked on the original live history files:

1. **Codex usage limit:** `task_complete` + `error.usage_limit_exceeded` → `STATUS_ABORTED`; error message fills `last_agent_msg` / `load_events` when `last_agent_message` is null. Same path covers 401 / provider `other` errors (non-empty `error.message`).
2. **OpenCode provider/abort errors:** `message.error` (`APIError` with `data.message` + `statusCode`, `MessageAbortedError`) → `STATUS_ABORTED`; error text fills `last_agent_msg` / `load_conversation` / `load_events` even when the turn has zero text parts (previously the turn was user-only on mobile).
3. **Claude 2.1+ upstream errors:** `system`-type entries carrying `error.formatted` / `error.status` (401, ECONNRESET connection drops — reproduced live 2026-09-23 with a dummy key, no account needed) → `STATUS_ABORTED`; retry bursts collapse to one trailing assistant error; titles still use the real prompt, never the error text.
2. **Pi weekly rate limit:** `stopReason=error` + `errorMessage` → `STATUS_ABORTED`; error text retained in list, plain conversation, and `load_events` (no longer user-only).
3. **Pi / OpenCode success paths** remain `STATUS_DONE` with the assistant reply.

Until consumers migrate, prefer `status_tag` + `last_agent_msg` together: do not treat `STATUS_DONE` alone as “notify job finished” without confirming the sample is not an older SessKit build.

## Verification (mandatory when changing parsers or load wiring)

Unit tests that **mock** `load_conversation` can stay green while the real registry wiring is broken. After any change to parsers, registry adapters, transcript parsing, status inference, or CLI show/export/share:

1. Run the fixture wiring tests (`tests/test_registry_load.py`) — they must exercise the **real** adapter, not a session-dict stub that hides path wiring.
2. On a machine with local history, sample **every installed runtime** that has sessions (not one Codex-only check):
   - `load_session_conversation` / registry load succeeds (no `TypeError` from passing a dict as a path).
   - Roles are only `user` / `assistant`; no empty text; no literal `"None"`.
   - Strong system / injection markers must not appear in plain user turns (e.g. `system reminder`, `<system`, `task-notification`, `<ide_opened_file`, `<agent_skills>`, `<mcp_file_system>`, `<function_calls>`).
   - Every plain user text appears in `load_events` `user_message` texts (allow substring match when share splits more finely).
3. **Fine-grained live runtime checks (required for SessKit maintenance, not optional polish):**
   - Prefer **fresh one-shot prompts** per installed runtime (install the CLI if missing; skip only when policy forbids install — e.g. removed Claude Code — or when auth/quota truly blocks, and record the skip).
   - For each runtime, verify **at least one success path** and, when reproducible, **one abnormal path** (usage limit, rate limit, forced error). Compare **native history** → SessKit `status_tag` / `last_agent_msg` / `load_events` on the **exact** session path (do not trust “matched N of scan window” without path/id equality).
   - Official docs / web search inform expected native fields; they do **not** replace a live run on this machine.
   - Hooks / notify / SSE are Corral integration concerns; SessKit verification still owns correct **parse of whatever landed on disk** after those runs.
4. If Corral consumes the same change: confirm Corral’s `sesskit_bridge` path and SessKit registry path return the **same** `(role, text)` sequences for the sampled sessions.
5. Do not claim “data correctness OK” from fixture-only runs when real history is available. Do not claim “abnormal endings OK” without a live failure sample (or a checked-in fixture cloned from one).
