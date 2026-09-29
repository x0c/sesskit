# Abstraction Redesign

Status: accepted direction (2026-09-29); implementation staged. This document owns
the target shape of SessKit's cross-runtime abstraction and the order in which it
is reached. Current behavior stays authoritative in `docs/CONTRACT.md` and
`docs/UNIFIED_ABSTRACTION_KNOWLEDGE_BASE.md` until each stage lands; research
evidence is collected in §6.

## 1. Problems in the current design

| # | Problem | Consequence |
|---|---|---|
| P1 | No runtime adapter interface. Scan, conversation load, activity load, and v1 transcript each dispatch on the runtime name in separate places; runtime capabilities are implicit. | Adding or fixing a runtime touches many files; consumers hard-code runtime names and private support tables. |
| P2 | Two interpretations of raw history: plain conversation still comes from per-runtime parsers, while typed activity is a separate interpreter (v1 transcript is already a projection of it). | Error policy drifts per runtime (one hides error-only turns, others emit them as assistant text). |
| P3 | `SessionInfo` is a flat record of 20+ fields mixing identity, metadata, display strings, previews, process observation, and a string outcome duplicated by the typed `SessionOutcome`. | Display formatting and liveness leak into the core record; outcome has two sources of truth. |
| P4 | No turn concept; outcome exists only per session. | Per-turn completion, interruption, and quota exhaustion cannot be expressed. |
| P5 | `ActivityEvent` is one dataclass with type-dependent optional fields; tool calls and results must be paired by every consumer. | Type checkers cannot catch misuse; pairing logic is duplicated downstream. |
| P6 | Missing concepts: permission requests and decisions, subagent/child sessions and forks, context compaction, model and token usage, attachments, injected system context, steering versus queued user input. | Consumers either lose the information or re-parse native history. |
| P7 | `AgentError.kind` is free text. | "Quota exhausted" and "completed" cannot be distinguished reliably; retryability is unknown. |
| P8 | Real-history parity checks are ad-hoc scripts rewritten per change; no conformance suite every adapter must pass. | Verification quality depends on whoever runs it. |
| P9 | Snapshot-only reading (addressed by the incremental reader stage). | Consumers poll by re-reading whole histories. |

## 2. Target design

- **Runtime adapter (P1).** Each runtime implements one adapter: `id`, `display_name`, `capabilities`, `scan(options)`, `open(session)` returning a reader, and snapshot load. `Capabilities` is a frozen, typed declaration, for example: incremental reading, structured questions, permission records, native tool-result status, native turn boundaries, subagent linkage, usage data. Consumers branch on capabilities, never on runtime names. The registry holds adapters; public entry points dispatch through it only.
- **Single interpretation (P2).** Typed activity is the only interpreter of raw records. Plain conversation, v1 transcript, list previews, and status tags are projections of it with one documented error policy.
- **Session record split (P3).** `SessionRef` (runtime, native id, history location), `SessionMetadata` (cwd, times, native title, relations), `SessionSummary` (previews, outcome), `ProcessObservation` (pid and its evidence). Display strings belong to consumers. The flat v1 record remains a compatibility projection until a versioned schema retires it.
- **Turns (P4).** A turn starts at a user input and ends at the next user input or end of history; runtimes with native turn records (Codex turns, Pi `turn_start`/`turn_end`, Claude result per turn, OpenCode steps) define the boundary instead. `TurnOutcome` states: `completed`, `awaiting_input`, `interrupted`, `failed`, `rejected` (the model refused; not an error), `in_progress`, `unknown`, with evidence, optional error, and the native stop reason kept verbatim next to the normalized value. The session outcome is derived from the last turn. A bare trailing user message is weak evidence: it may yield `in_progress` with inferred evidence, never `awaiting_input`.
- **Typed events (P5).** Events are a tagged union of per-kind types sharing `seq`, `ts`, `message_id`, `turn_id`, and `evidence`; native raw payloads stay attached. Target kinds: `user_message`, `assistant_message`, `thinking`, `tool_call`, `tool_result`, `interaction_request`, `interaction_resolution`, `error`, `compaction`, `system_context`, `attachment`, `usage`, `plan_update`, `queued_input`, `lifecycle`. A paired `ToolInvocation` view is provided by SessKit with status `proposed`, `succeeded`, `failed`, `denied` (refusal by a person or policy, never `failed`), `awaiting_approval`, `timed_out`, `unknown`; missing evidence is `unknown`, never success.
- **Error taxonomy (P7).** Closed kinds: `rate_limited`, `quota_exhausted`, `auth`, `context_length`, `provider_overloaded`, `provider_error`, `policy_blocked`, `tool_error`, `user_interrupt`, `timeout`, `runtime_crash`, `unknown`; plus `scope` (`tool`, `turn`, `session`), native `code` and `http_status` only when natively present, raw message, `retryable` (true, false, or unknown), and evidence. Unclassifiable errors are `unknown`, never guessed. Refusals and denials are not errors.
- **Interactions.** One request model covers `question`, `permission`, `plan_approval`, `elicitation_form`, and `elicitation_url`. Resolution: `pending`, `answered`, `approved`, `denied`, `declined`, `dismissed`, `expired`, `unknown`, plus `decided_by` (`human`, `policy`, `unknown`) and `is_secret` / `is_blocking` where native. Approval polarity and explicit "no" are never folded into dismissal. SessKit models requests and resolutions as data only; submitting answers stays with the host.
- **Added concepts (P6).** Added only where native history provides evidence, each behind a capability flag: session relations (`subagent`, `fork`, `continuation`, with lineage validation that quarantines cycles, self-parents, and malformed links; a superseded session keeps its record with a `superseded_by` marker and chain resolution stops at missing targets), usage ticks per step/turn with model attribution (cost only when natively recorded; pricing is a consumer projection), compaction records (trigger, summary reference, first kept entry), system/injected context kept out of plain conversation, attachments, plan items with replace-all semantics, and steering versus queued input.
- **Incremental cursors (P9).** Opaque, versioned cursor holding a content fingerprint (head checksum plus file identity, not inode alone), the committed offset of the next unread complete record (committed after processing), and runtime-specific auxiliary state (Pi leaf and chain, SQLite row id). Truncation, replacement, or fingerprint mismatch starts a new generation and resyncs; it never continues silently.
- **Conformance (P8).** A shared conformance suite runs every adapter against synthetic fixtures (ordering, pairing, evidence, error, and outcome invariants). A `verify` CLI command runs real-history parity and timing over local sessions and prints counts only, never content.

## 3. Invariants

- Unknown stays unknown: no success, answer, or completion is inferred without evidence.
- v1 transcript output and the CLI envelope stay byte-compatible until a versioned schema change.
- SessKit remains host-neutral: no host names, paths, claims, or policies in core. Host behavior enters only through per-call extensions.
- Kimi typed-activity adaptation is deferred; existing Kimi behavior is unchanged.

## 4. Stages

| Stage | Scope | Depends on |
|---|---|---|
| A | Conformance suite and `verify` command (P8) — landed 2026-09-30: `src/sesskit/conformance.py` + `tests/test_conformance.py`, `sesskit verify` as the real-history gate; 2026-09-30 fix: unmatched-call check scoped to the terminal turn, Pi reader emits compaction linkage like the snapshot | — |
| B | Adapter protocol, capabilities, adapters delegating to existing functions (P1) — landed 2026-09-30: `src/sesskit/adapters/` (`RuntimeAdapter`, frozen `Capabilities`, one adapter per runtime), `get_adapter`/`list_adapters` in the public API, capabilities in `sesskit describe` | — |
| C | Turn model, error taxonomy, typed event union and tool invocation view, additive (P4, P5, P7) — landed 2026-09-30, aligned to research 2026-10-01: `ErrorKind` (+`policy_blocked`, `timeout`) + `classify_error`/`native_http_status` (`errors.py`), `Turn`/`TurnOutcome` (`completed`/`awaiting_input`/`interrupted`/`failed`/`rejected`/`in_progress`/`unknown`, verbatim `stop_reason`, `pending` alias) + `derive_turns`/`derive_session_outcome`/`pair_invocations` (`turns.py`; invocation status `proposed`/`succeeded`/`failed`/`denied`/`awaiting_approval`/`timed_out`/`unknown` + `pairing`), per-kind views + `as_typed` and `turn_id`/`retryable`/`scope`/`http_status` additive fields (`models.py`), `AgentError.scope`/`http_status`, `InteractionRequest` polarity (`approved`/`denied`/`declined`, `decided_by`, `is_secret`/`is_blocking`; `elicitation_form`/`elicitation_url`), `tests/test_turns_errors.py` | — |
| D | Incremental reader (P9), Pi first | — |
| E | Host-policy extraction behind per-call extensions | — |
| F | Registry and public entry points dispatch via adapters (P1 wiring — landed 2026-09-30); conversation becomes an activity projection (P2 conversation — landed 2026-09-30: `conversation_from_activity` + `project_session_conversation`, five runtime adapters project, Kimi stays legacy; list previews and status tags stay on bounded-tail scans for speed) | B, D, E |
| G | Producers emit turns, the taxonomy, and new concepts (P6) — new concepts landed 2026-09-30: `SessionRelation` + `session_relations` (`relations.py`), `Usage` on assistant events, `compaction` event kind + `CompactionInfo` markers, `origin` (human/injected/system/unknown) on user events, `compaction_markers` + `message_origin` capability flags (all five typed runtimes; Kimi deferred); turn/error remainder landed 2026-10-01: native `turn_id` stamping (Codex item/terminal turn ids; Pi/Claude/OpenCode keep derived ids — no native per-message turn record observed), typed-only `lifecycle` terminal markers (Codex `turn_aborted`/bare `task_complete`, OpenCode empty tail rows), classified event errors with `scope` + `http_status`, Pi `stopReason` verbatim + text-riding errors, Codex `verified_answer` linkage (`decided_by=human`), one shared `visibility.py` predicate for conversation + v1 | C, F |
| H | Session record split with versioned schema (P3) | F |

Each stage lands with conformance fixtures, real-history parity for affected runtimes, and no timing regression on the `verify` report.

## 5. Open decisions

- Whether `live`/`pid` stay in the stable record or move to `ProcessObservation` only (tied to stage H).
- The public name and versioning of the typed JSON schema that follows v1.

## 6. Research evidence

Collected 2026-09-30 from official documentation and shallow clones of source at the commits noted. Findings drive §2; items marked "community" are unconfirmed and must not justify a capability flag on their own.

### 6.1 Native runtime models

| Concept | Claude Code / Agent SDK | Codex CLI | OpenCode | Cursor CLI | Pi | ACP |
|---|---|---|---|---|---|---|
| Turn record | One result message per turn with subtype and terminal reason | `Turn` with `TurnStatus` and abort reason | Step start/finish parts (turn fallback) | Terminal result event in stream output | `turn_start`/`turn_end` events, `stopReason` | Prompt turn ends with `StopReason` (includes `cancelled`, `refusal`) |
| Tool lifecycle | `tool_use` / `tool_result` with `is_error` (often absent in persisted history) | Items in progress/completed/failed/declined | `ToolState` pending/running/completed/error | Tool call started/completed events | `toolCall` / `toolResult` | `ToolCall` status pending/in_progress/completed/failed |
| Errors | Assistant message error field, API error status, rate-limit info | `CodexErrorInfo` variants, `affects_turn_status` | `AssistantError` union (auth, API, output length, aborted, context overflow) | Result `is_error` | `errorMessage` plus `stopReason` | JSON-RPC errors; cancellation is a stop reason, not an error |
| User questions | `AskUserQuestion` tool | `request_user_input` (also used for approvals), secret/blocking flags | `question` asked/replied/rejected events | `cursor/ask_question` over ACP | None found (extensions only) | Elicitation, separate from permission |
| Permission | Permission modes, `permission_denials` | `ReviewDecision` (approved, approved for session, denied, abort, timed out) | `permission` asked/replied (`once`, `always`, `reject`) plus rulesets | `--force` bypass; no persisted record verified | None found | `request_permission` with four option kinds plus `cancelled` |
| Relations | `parentUuid` chain, sidechain/subagent transcripts, `parent_tool_use_id` | Thread fork/resume, sub-agent threads | Session `parentID`, subtask/agent parts | Subagents (docs) | Tree `id`/`parentId`, `parentSession`, branch summaries | Session fork (RFD) |
| Compaction | Compact boundary and summary records | `compacted` records | Compaction part (auto, overflow, tail start) | Unknown | `CompactionEntry` with summary and first kept entry | — |
| Usage | Per-message usage, model usage, total cost | Token count events, thread usage | Per-step tokens and cost, session rollup | Unknown | Per-message usage entries | `UsageUpdate` |
| Steering / queue | Queued turn count, interrupt | `turn/steer`, queue changes | No record found | No record found | `steer` / `followUp` with dispositions | — |

Sources: Claude Agent SDK [agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop), [TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript), [user input](https://code.claude.com/docs/en/agent-sdk/user-input), [sessions](https://code.claude.com/docs/en/sessions); Codex `codex-rs/app-server-protocol` at `openai/codex@65c3f40`; OpenCode `packages/opencode/src/session/message-v2.ts` at `sst/opencode@7945de2`; Cursor [output format](https://cursor.com/docs/cli/reference/output-format), [subagents](https://cursor.com/docs/subagents), community notes on local storage ([forum](https://forum.cursor.com/t/chat-history-folder/7653)); Pi `packages/coding-agent/docs/session-format.md` at `badlogic/pi-mono@02eed88`; [Agent Client Protocol](https://agentclientprotocol.com) schema v1 at `agentclientprotocol/agent-client-protocol@c254e00`, [session fork RFD](https://agentclientprotocol.com/rfds/session-fork).

### 6.2 Cross-agent references

- **Capability declaration.** vibe-kanban declares per-executor capabilities and normalizes each executor's log into shared entry types with a tool status enum (`crates/executors/src/executors/mod.rs`, `crates/executors/src/logs/mod.rs` at `BloopAI/vibe-kanban@d5cbb53`). Adopted: explicit capabilities, absent means unknown. Not adopted: spawn, approval-service, and display-oriented action types in the core model; its live patch-stream transport (it has no resumable cursor).
- **Adapter layout at scale.** [ccusage](https://github.com/ccusage/ccusage) (19 runtime adapters, `rust/adapters/` at `7f32aaf`) keeps a per-runtime `paths/parser/loader/types` layout with fixture tests, but has no capability declaration, and runtime differences leak into CLI branches. Adopted: layout, fixture harness, Pi lineage quarantine (`adapters/pi/src/loader.rs`), cost as a separate projection. Not adopted: convention-only adapters.
- **Outcome and refusal states.** A2A task states distinguish input-required, auth-required, rejected, canceled, and failed (`specification/a2a.proto` at `a2aproject/A2A@72b3761`; [overview](https://agent2agent.info/docs/concepts/task)). AI SDK tool parts separate `output-error` from `output-denied` and record approval reasons and automatic decisions ([UI message](https://ai-sdk.dev/docs/reference/ai-sdk-core/ui-message), [tool usage](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-tool-usage), [tool approvals](https://ai-sdk.dev/docs/agents/tool-approvals)). MCP elicitation distinguishes accept, decline, and cancel, and form versus URL modes ([spec](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)); ACP mirrors this ([elicitation](https://agentclientprotocol.com/protocol/v2/elicitation)). Adopted: refusal and denial are never errors; resolution keeps polarity.
- **Event vocabulary.** AG-UI defines lifecycle, text, reasoning, tool call, and interrupt events with shared ids and resumption without re-emission ([events](https://docs.ag-ui.com/concepts/events), [interrupts](https://docs.ag-ui.com/concepts/interrupts), [subagents](https://docs.ag-ui.com/concepts/subagents)). OpenTelemetry GenAI conventions provide usage and conversation vocabulary and a compaction marker, but only `error.type` is stable ([overview](https://opentelemetry.io/blog/2026/genai-observability)). Adopted: vocabulary and id-based grouping. Not adopted: OTel attribute names as SessKit fields; streaming start/content/end assembly (persisted history does not need it).
- **Incremental reading.** Log shippers identify files by content fingerprint rather than inode alone, commit offsets after processing, and reset on truncation ([Vector file source](https://vector.dev/docs/reference/configuration/sources/file), [Fluent Bit tail](https://docs.fluentbit.io/manual/data-pipeline/inputs/tail)). Adopted in the cursor design (§2).
- **Schema evolution.** Keep versioned JSON schemas with additive evolution, open metadata maps, and raw payload preservation; code generation without versioned schema files (vibe-kanban) is not adopted.

### 6.3 Stage G local-history evidence (2026-09-30)

Counts from a bounded scan of this machine's histories (shapes only, never
content). v1 parity (`to_v1_dicts(load_activity(s)) == load_events(s)`) holds
on every sampled session; projected v1 `seq` values are renumbered densely so
previously dropped rows surfacing as typed-only events do not shift v1 bytes.

| Runtime | Relations | Usage / model | Compaction | Injected context |
|---|---|---|---|---|
| Claude | `Agent` tool calls (3 in 20 files); `continued-in` forward pointers; `parentUuid` chains every row | `message.usage` + `model` on 8224/8224 assistant rows (57 sessions); `cost-state` rows carry `totalCostUSD` + per-model tokens | `isCompactSummary` rows (3 in 57 sessions) | `queued_command` admits only explicit human origin; wrapper-only rows surface as `injected`; missing-origin rows stay `unknown` |
| Codex | `thread_source` (`user` 119, `voice_chat` 1 in 120 files; `subagent` handled) | `token_usage_record` in 54/60 files (per-turn input/cached/output/reasoning/total + turn/thread ids); model strings per message | `compacted` rows (77 in 120 files) with `replacement_history` | `# AGENTS.md instructions` / `<environment_context>` user rows (217 lines) |
| OpenCode | `session.parent_id` (70 v2 + 71 v1 rows), `fork_session_id` (0 local), `subtask` parts with agent/model; scanner lists top-level (`parent_id IS NULL`) sessions only | Session rollup (`cost` > 0 in 189/519, tokens in 414/519) plus per-message `modelID`/`model` + `tokens` + `cost` | `compaction` part type + `time_compacting` column | `system`/`synthetic` parts skipped; no injected user markers observed |
| Cursor | `subagentInfo` parent mechanism (0 of 200 local sessions) | No native per-message usage (only `modelProviderMessageId`) | None observed | `<user_info>` blocks in 58/60 sessions; `<user_query>` marks genuine input |
| Pi | `subagents:record` custom entries (57) with native child ids | `usage` + `model` + `provider` on 11796/11796 assistant messages (100 sessions); cost inside `usage.cost.total` | `compaction` entries (4 in 188 sessions) with `summary` + `tokensBefore` + `usage` | `user` role is human input; system rows carry no text locally |

Prevalence notes, not runtime-wide claims: Codex turn-level usage attaches
only where a `token_usage_record` turn id matches a message turn (370 of 2295
assistant events in 120 sessions); OpenCode plain `parent_id` without fork
fields keeps kind `unknown`; Kimi behavior is unchanged by scope decision.
