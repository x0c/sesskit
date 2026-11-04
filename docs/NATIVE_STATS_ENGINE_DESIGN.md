# Native stats engine design

Status: adopted 2026-11-04 (owner decision, consumer requirement from the
Corral `agentflex` card); implementation in progress under `crates/`.

## 1. Purpose and boundary

A multi-core Rust engine that computes a bounded **activity and usage
projection** over a time window across runtimes, fast enough for an
interactive command (target: the default 7-day window in under 1 second on a
cold run). It exists because the Python reference implementation reads and
fully interprets every session twice and takes 15–25 s for the same window,
and because a consumer distributed through `npx` cannot run Python.

The engine is part of SessKit, not a consumer parser: it implements the same
native-history interpretation rules as the Python reference for the projection
it serves, and it is verified against that reference on real histories. It
does not replace the Python API, transcripts, outcomes or the full usage
snapshot. Presentation (cards, animation, images) stays downstream.

## 2. Projection

For each session with activity inside `[start, end]`:

- runtime, session id;
- activity timestamps of conversation records inside the window (user,
  assistant, tool call/result rows; metadata, system and sidechain rows
  excluded, as in the activity projection);
- reconciled usage records with the **timestamp of the native record that
  carries them**, so in-window tokens are attributed by time.

Aggregates derived from it (sessions per runtime, in-window token total,
active intervals, hour-by-weekday histogram) are pure functions over this
projection.

### Why records carry timestamps

The Python usage snapshot carries no time per record. Selecting in-window
usage through activity-event message ids does not work: Pi activity events
use a composite message id and Claude activity events carry none, while usage
records use native entry/message ids. A consumer that selected by message id
(the first Corral wrap version, 2026-11-04) reported every session that
started before the window as "without token data". Attributing each reconciled
record to its native record time removes that mismatch.

## 3. Interpretation rules (must equal the Python reference)

Usage collection, normalization, reconciliation and the `known_total` total
follow `src/sesskit/usage.py`:

- Collectors per runtime: Claude message usage (fragments of one response
  share `message.id`), Claude `cost-state` session rollups; Codex
  `token_usage_record` (`usage` call detail, `turn_token_usage` turn
  cumulative, `thread_token_usage` session cumulative) and legacy
  `event_msg.info` blocks; Pi message usage (assistant = message scope, other
  roles and compaction/branch-summary/independent carriers = unknown scope);
  OpenCode v1 `message` / v2 `session_message` token data plus session rollups.
- Normalization per runtime (`_normalize_*`), including Codex's
  reported-total-only rule.
- Reconciliation keys (`_record_key`): response id; cumulative turn counter;
  cumulative session counter (+ model); scope id; otherwise an ambiguous
  record that never enters sums. Equal duplicates collapse, cumulative
  revisions fold to the latest, differing absolute values conflict and are
  excluded.
- `known_total` for total tokens: firm call/message/turn details grouped by
  turn; a turn total overlapping finer details is set aside; a cumulative turn
  interval contributes its residual over details; details exceeding their
  interval exclude that turn; session rollups never enter the sum.

Record time: the native row timestamp of the observation (first observation
for collapsed duplicates; latest revision for folded cumulative counters).

## 4. Performance design

- Discovery by directory listing and file `mtime` only; no per-session
  metadata parse. OpenCode rows are selected by indexed time columns.
- Files are processed in parallel across cores; JSONL lines are filtered by
  cheap byte checks before JSON decoding, and only the fields of the
  projection are decoded.
- Window pruning: JSONL histories are append-ordered, so a file whose window
  starts after its beginning is read from the first in-window offset found by
  scanning backwards from the end.
- An incremental per-file summary cache (keyed by device, inode, size and
  modification time; append-only growth resumes at the stored offset) makes
  repeat runs proportional to new bytes. Owned by the consumer's cache
  directory, never written into runtime history directories.

## 5. Verification

- Parity harness: for every session whose whole history lies inside the
  window, the engine's total must equal Python `load_usage(...).summary
  .total_tokens.known_total`; sessions and in-window activity counts are
  compared against the Python activity projection. Mismatches are listed per
  session, never averaged away.
- Timing on the owner's real 7-day window, cold (fresh process, no engine
  cache) and warm; report file count and bytes.
- Platform coverage: macOS, Linux and Windows builds; Windows on real
  assistant histories (Corral design §10.5).

## 6. History discovery per OS (researched 2026-11-04)

Every runtime resolves its root from the user's home directory, which is
`HOME` on macOS/Linux and `%USERPROFILE%` on Windows (Rust `dirs`/Node
`os.homedir()`); separators are platform-native. Overrides, highest first:

| Runtime | Default root | Overrides |
|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/<id>.jsonl` | `CLAUDE_CONFIG_DIR` replaces `~/.claude` ([docs](https://code.claude.com/docs/en/env-vars)) |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>[_<rollout>].jsonl` | `CODEX_HOME` replaces `~/.codex` ([source](https://github.com/openai/codex/blob/d84c872464aca4821dc5cb548a651651aefd84fa/codex-rs/utils/home-dir/src/lib.rs#L5-L63)); after a revert the file name carries `_<rollout id>` after the thread id, which stays the session id ([source](https://github.com/openai/codex/blob/d84c872464aca4821dc5cb548a651651aefd84fa/codex-rs/rollout/src/rollout_file_name.rs#L62-L74)) |
| OpenCode | `<data>/opencode/opencode.db`, `<data>` = `XDG_DATA_HOME` or `~/.local/share` on every OS | `OPENCODE_DB` (file name or path; `:memory:` means none); channel builds use `opencode-<channel>.db` beside it ([source](https://github.com/anomalyco/opencode/blob/fe046564650854594c4c5aa22270f46d9de6cace/packages/cli/src/database-path.ts#L4-L12)). `OPENCODE_DATA_DIR`, which the Python parser honors, does not exist in current OpenCode source |
| Pi | `~/.pi/agent/sessions/<cwd-slug>/<ts>_<id>.jsonl` | `PI_CODING_AGENT_SESSION_DIR` (sessions), `PI_CODING_AGENT_DIR` (agent root) ([source](https://github.com/badlogic/pi-mono/blob/676af1d777ef6f04b6bc8daf4cf3b9b1799cff4d/packages/coding-agent/src/config.ts#L602-L611)) |

The Python parsers currently hard-code the defaults (except OpenCode's
`XDG_DATA_HOME`/`OPENCODE_DATA_DIR`); honoring these overrides there is an
open SessKit improvement. The native engine honors them. Cursor's agent CLI
store location is unverified and out of the engine's scope.
