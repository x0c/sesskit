# Native stats engine design

Status: adopted 2026-11-04 (owner decision, consumer requirement from the
Corral `agentflex` card); implemented in `crates/sesskit-stats`, parity
verified (§7).

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

### Shared time aggregates (`sesskit_stats::aggregate`, 2026-11-08)

The time-derived aggregates live in the crate so every consumer (AgentFlex,
Corral, future cards) gets the same numbers; consumers call them instead of
re-implementing the rules (owner request 2026-11-08, after AgentFlex carried
its own copy):

- **Active intervals:** a session's ascending activity timestamps merge into
  one interval while consecutive records are at most 300 s apart (Corral
  policy constant). Each interval then extends 60 s past its last record, so
  a lone record still occupies time and two sessions answering at the same
  moment overlap. The first-to-last span is wrong: sessions left open for
  days inflated an early peak to 20.
- **Peak concurrency:** the maximum number of intervals, across all sessions,
  open at one instant, plus the first instant it is reached. Intervals that
  only touch (one ends exactly when another starts) are not concurrent.
- **Weekday × hour histogram:** interval seconds split at local hour
  boundaries for a caller-supplied UTC offset, indexed Monday = 0, hour 0–23.

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

## 7. Listing rules, activity definition and parity results (2026-11-04)

Session set (a session is excluded with an explicit reason, never silently):

- `internal_agent`: Claude teammate/subagent files (head rules of
  `_is_internal_claude_session`), Codex `thread_source = "subagent"`.
- `missing_cwd` / `ephemeral_marker`: missing or deleted working directory, or
  a `.sesskit-ignore` marker, for **every** runtime. The Python OpenCode and Pi
  listings do not apply this drop yet (open SessKit gap; the AGENTS.md rule
  says resume-style listings drop missing cwd).
- `empty_history`: no real user prompt — Claude non-meta user text outside the
  local-command wrappers (`_SKIP_PREFIXES`); Codex `event_msg.user_message` or a
  `response_item` user message that is not injected context
  (`user_message_text`); Pi a user message on the active branch
  (`active_messages`: leaf = last id-bearing entry, walk `parentId`; top-level
  fields are scanned without trusting the first `"id"` substring, because
  custom rows nest other ids before the top-level one).
- OpenCode: top-level (`parent_id IS NULL`), unarchived sessions; databases are
  deduplicated by session id.

Activity timestamps are **record rows** (Claude user/assistant rows, Codex
`response_item` and user/agent `event_msg`, Pi active-branch
user/assistant/toolResult rows, OpenCode message rows), not the typed activity
events of `load_activity`; counts differ by design, times agree. Consumers use
only the times (intervals, heatmaps), never the counts.

Performance design as built: a top-level JSON field scanner (SIMD string
skipping, early exit) decides per row; full decoding happens only for rows
that can carry usage (`"usage"`/`"model"` in the message, Codex usage records)
and for user rows until the first prompt is found. OpenCode is read on
parallel read-only connections with memory-mapped I/O, rows are fetched in
index order (`session_id, time_created`) and ordered by `seq` in Rust, because
sorting the large v2 rows by `seq` in SQL spilled about 0.9 GB into a temporary
B-tree; only `tokens`, `cost`, `modelID`, `model.id` and ids are scanned per
row. Measured warm on the owner's machine (load average 6–10): 7 days
0.27–0.30 s, 30 days 1.4–1.6 s, all history about 4 s; repeat runs beyond 7
days await the incremental cache (§4).

Accepted listing difference: the Python Claude listing hides sessions whose
only prompt is a low-value title (for example a lone greeting) as title noise;
the engine keeps them because they hold a real prompt. The three such sessions
found on 2026-11-04 report zero tokens because Claude Code never called the
model: their only assistant rows are local `<synthetic>` errors ("Login
expired", "You've hit your weekly limit") with all-zero usage. A greeting that
reaches the model always consumes input (system prompt, context, the message)
and output tokens; zero is evidence of no model call, not of a cheap one.

Parity on the owner's real histories (7-day window, harness
`crates/sesskit-stats/scripts/parity.py`, Python run against the committed
snapshot): total tokens 373/373 sessions exact (after the performance rework:
375/375 for 7 days and 1,073/1,073 for 30 days); pruned versus full reads
0 differences over 379 sessions (1,090 for 30 days); session set identical except 27 sessions the
Python OpenCode/Pi listings keep despite a deleted cwd. Sessions written during
the run can differ transiently; frozen copies matched exactly. A 12-session
independent spot check of finished sessions matched exactly. Timing: 0.28–0.47 s
warm, about 1.1 s on the first cold run after new files appear.
