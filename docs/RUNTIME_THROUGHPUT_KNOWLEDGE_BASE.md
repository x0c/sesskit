# Runtime throughput knowledge base

Research baseline: 2026-11-01. Read-only investigation of six runtimes, including
official web documentation, runtime writers and existing local histories.
**This is evidence for future implementation, not a shipped speed API.** No parser,
usage schema, runtime setting or native history was changed.

This document owns native timing locations, interpretation limits and the verified
SessKit gaps. The [native usage design](NATIVE_USAGE_DATA_DESIGN.md) owns the proposed
usage model/reconciliation; the [runtime parsing KB](RUNTIME_PARSING_KNOWLEDGE_BASE.md)
owns history loading. General recorded-usage evidence rules remain in the shared
[session integration guide](~/.config/agentsync/docs/AGENT_SESSION_INTEGRATION_GUIDE.md#recorded-usage-and-estimates).

## Conclusion and measurement vocabulary

Historical throughput is feasible for **Pi, OpenCode and Kimi** in the checked
formats, but their denominators differ. Claude exposes record-to-reply timing
evidence with unverified endpoint/finality semantics; Codex exposes turn timing,
not a verified per-call generation window. Cursor has no verified historical
usage/timing mapping in the parsed sources.

No built-in numeric TPS measurement was found in the inspected persisted shapes.
Dividing native counters by native elapsed time produces a **derived rate**,
not a provider-reported measurement.

| Measurement | Interpretation |
|---|---|
| Request/response elapsed throughput | Output tokens divided by a named client response interval; can include queueing, prefill, network, retries and client work. |
| Client-observed stream throughput | Output tokens divided by an observed streaming interval. Chunking, non-text parts and client scheduling affect it. |
| Turn wall throughput | Turn output divided by turn elapsed time, including tools and between-call work; not model generation speed. |
| First-output latency | An explicitly defined first-output event minus the source's start; not necessarily first text token or provider TTFT. |
| Provider decode throughput | Requires evidence for provider-side generation timing; none of these client clocks establishes it. |

For a compatible positive duration in milliseconds, the arithmetic is
`output_tokens * 1000 / duration_ms`. Identity, coverage, output semantics and
the duration's endpoints are part of the answer, not optional labels.
Missing timing/usage, invalid durations and unverified finality are not zero speed.

## Checked formats at a glance

Versions identify the inspected installation, not the first supported version.
Detect fields per record; no runtime-wide completeness or historical version floor
was established.

| Runtime | Installation checked | Persisted evidence | Defensible historical result |
|---|---|---|---|
| Pi | 1.4.0 | Assistant `message.durationMs`, `message.timestamp`, `message.usage.output` | Per-response client elapsed throughput where timing and trustworthy usage exist. No stored first-token time. |
| OpenCode | 2.2.5; v1/v2 SQLite stores | Assistant `time.created`, v2 `time.streamed`, `time.completed`, token buckets | Provider-phase or whole-step client elapsed throughput on proven same-attempt terminal rows. Not decode-only. |
| Kimi Code | 2.4.1 | Nested `step.end` usage and `llm*Ms` timing | Per-step client stream throughput, plus native client first-part latency. |
| Claude Code | 2.1.316, with 2.1.296 rows also sampled | `api-request.id/timestamp` joined through `assistant.requestRef`; reply timestamps; cumulative `cost-state` durations | Recorded request-to-reply-flush intervals and conditional observed averages. Exact generation endpoints and counter finality remain unverified. |
| Codex CLI | 0.166.0 | Response usage/id; `task_complete.duration_ms` and `time_to_first_token_ms` | End-to-end turn average and native turn first-output latency. No verified historical per-call decode denominator. |
| Cursor Agent CLI | 2026.10.22-27dcf5f | Parsed store blobs/transcripts lack usage plus model timing | Historical rate mapping unsupported for verified parsed formats; binary per-blob coverage remains unverified, not evidence of absent data. |

## Pi

Sources: [assistant types][P1], [stream timer][P2],
[session format][P3], [message types][P4].

- `AssistantMessageEventStream` measures `durationMs` with a monotonic clock
  from stream construction to terminal `done`/`error` or `end(result)`.
  Existing durations are retained; messages whose timestamp predates the stream
  can remain untimed, including deferred results fetched later.
- `message.timestamp` is documented as request-start wall time. Stream placement
  and monotonic-versus-wall clock differences prevent treating
  `timestamp + durationMs` as an independently measured exact wall endpoint.
  The session entry's ISO timestamp is **append time**, not stream-end time.
- Use `usage.output` without adding `usage.reasoning`: the latter is a subset
  in this verified Pi schema. Tool-call-only responses can have substantial
  output usage. Error/abort zero counters need finality evidence.
- Neither a first-token timestamp nor a built-in rate is persisted in the
  checked core shape. Community extensions can persist their own telemetry;
  those records need separate extension identity/provenance, not relabeling as
  native provider measurements. `tokensPerSecond` on the faux model is a
  simulated-playback setting, not measured speed.

The existing SessKit Pi collector preserves usage but drops response timing.

## OpenCode

Sources at one inspected writer pin: [runner][O1], [message projection][O2],
[usage mapping][O3], [assistant schema][O4]; [official CLI stats docs][O5].

- `time.created` originates at step/request setup; `time.streamed` is projected
  after the provider body drains, **not** at first token. `time.completed`
  follows tool settlement and terminal processing.
- `streamed - created` is a client provider-phase interval, not pure decode.
  It can include snapshot work, transport, backpressure and overlapping tool
  activity. `completed - created` additionally includes post-stream tool work.
  The v1 sample lacked `streamed`; do not invent it.
- The verified mapping stores visible `tokens.output` and `tokens.reasoning`
  separately. Inclusive output is their sum; a visible-output rate must be
  labeled differently. Input cache buckets are not output addends.
- **Stale-pairing hazard:** `session.step.started` can reset an existing
  assistant's timing/finish/error/native fields without clearing tokens/cost.
  `session.step.failed` only replaces usage when both tokens and cost exist.
  A failed or in-flight row can therefore combine new timing with old usage.
  In the inspected writer, successful terminal projection sets finish, timing
  and usage together. A conservative pairing set requires successful
  `stop`/`tool-calls`, no error/retry, valid usage and compatible positive
  timestamps. This is a version-scoped verification condition, not a universal
  API filter or reason to discard failed observations.
- The checked provider `native` metadata lane held only response ids.
  First-output timing was attached to a client telemetry span, not the checked
  persisted message. Future provider metadata/store versions remain unverified.

SessKit currently retains the usage buckets but not these timing endpoints.

## Kimi Code

Sources: [official data locations][K1], [usage schema pin][K2], and installed
`@moonshot-ai/kimi-code@2.4.1` writer module
`packages/agent-core-v2/src/llm-adapter/model/model-requester-impl.ts`
(`buildStreamTiming`, `runRequest`, `finalizeDecodeStats`) plus
`finishMachineStepProjection` in the bundled machine loop.
The public schema pin establishes usage shape only; timing semantics here were
verified in the installed writer and native records, not inferred from that pin.

- The primary historical source is **not just `usage.record`**:
  `context.append_loop_event.event` with `type: "step.end"` can carry
  `usage`, `turnId`, `step`, `uuid`, `messageId` and
  `llmStreamDurationMs`/`llmFirstTokenLatencyMs` and related timings.
- `llmStreamDurationMs` measures first streaming part to `llm.done` on the
  client. The runtime's own debug formatter divides `usage.output` by this
  duration. Its 50 ms display threshold is display policy, not a SessKit
  accounting requirement.
- `llmFirstTokenLatencyMs` is client request-entry to first streaming part
  **of any kind**, not proven provider TTFT or first visible text token.
- **Misleading names:** `llmServerDecodeMs` accumulates client inter-part
  await/resume gaps; `llmServerFirstTokenMs` also uses client wall clocks.
  Neither is provider-reported server execution time. Preserve native names
  as provenance, not as proof of measurement origin.
- Error/interrupted sample steps had no timing or usage. A terminal step alone
  does not prove counters were measured: the writer also has empty-usage
  defaults. Require trustworthy, compatible usage before calculating a rate.
- `usage.record` is emitted after successful model requests. Its native
  `usageScope: "turn"` can denote a **step within a user turn**, not one whole
  user turn; `"session"` can denote non-turn operations such as compaction,
  not automatically a cumulative counter. Do not infer accounting scope from
  that label alone or double-count the same usage via `step.end` and
  `usage.record` without established linkage.
- Output reasoning inclusion differs by provider mapping. In the inspected
  Google mapper `thoughtsTokenCount` is not added to `output`; other paths
  pass provider output through. No uniform reasoning-subset rule is proven.
- Session diagnostic `logs/kimi-code.log` can duplicate the same timing and
  output values. It is a secondary, rotation-limited source, not live-only
  data. Preserve disagreements rather than silently overwriting either source.

SessKit currently reads `usage.record` counters but ignores nested step timing
and does not establish this finer call association.

## Claude Code

Sources: [official SDK accounting][C1], [SDK result types][C2],
[fast-mode usage semantics][C3], and the checked local JSONL shapes.
The writer is closed; no writer commit was inspected.

- Native `api-request.id` joins `assistant.requestRef` in the checked current
  histories. Preserve the request-record timestamp and each linked persisted
  reply timestamp, deduplicating usage by native response/message identity.
  A measured record-to-reply interval is useful; the exact request dispatch,
  first token and last token semantics of these writes are **not verified**.
  First reply-block persistence must not be renamed TTFT.
- SDK docs warn about per-step output placeholders; older issue reports show
  that behavior. This is not evidence that every current persisted JSONL has
  placeholders. Current sampled output counts were larger and an independently
  reconciled native message sum agreed with a rollup in one complete session.
  Neither large counts nor same-writer rollup agreement proves provider
  finality. Preserve version/row-kind quality and conflicts.
- `cost-state` can contain `totalDuration`, `totalAPIDuration`,
  `totalAPIDurationWithoutRetries`, `totalToolDuration` and model totals.
  Presence varies. Inclusion of idle, child, auxiliary or restored work is
  not established merely because SDK fields have similar names.
- `system.turn_duration.durationMs` and `thinkingDurationMs` are useful
  native evidence with incompletely verified spans. Do not pair a whole
  response's output with only thinking time.
- `usage.speed` (`fast`/`standard`) is a configuration echo, not measured TPS.

SessKit ignores `api-request` timing/linkage and the cumulative/turn/thinking
durations; current usage models do not provide their typed time evidence.

## Codex CLI

Sources: [turn clocks and first-output triggers][X1], [usage accumulation][X2],
[rollout persistence policy][X3], [item lifecycle emission][X4],
[analytics profile sink][X5], [exec JSON projection][X6],
[official non-interactive docs][X7].

- `token_usage_record` has response usage plus `response_id` and separate
  turn/thread counters. Turn timing is on `event_msg/task_complete`:
  `duration_ms` and `time_to_first_token_ms`; abort markers can carry duration
  without first-output latency. These optional fields need per-record detection.
- Pair a turn duration with the **latest counter of that same turn** or proven
  non-overlapping request usage. Turn counters reset; subtracting the previous
  turn's counter is incorrect.
- The native first-token label means first model output, including some tool
  call items. It is turn-scoped, not a per-call first-text-token measurement.
  `duration_ms - time_to_first_token_ms` still contains tools and later call
  latencies; it is not a decode window.
- `item_completed` timing is unsafe as a generation denominator. The inspected
  `record_response_item_and_emit_turn_item` path persists a full response item
  before emitting its start/completion lifecycle pair.
- The writer has a `TurnProfile` with sampling/tool/compaction phase clocks,
  but the inspected path sends it to analytics, not persisted rollouts.
  A client sampling phase would still not establish provider decode time.
- Internal/app-server streams can expose deltas that the rollout drops.
  `exec --json` is a reduced projection and drops those deltas as well;
  neither its documented usage counters nor live internal events establish
  historical first/last-token timing.

SessKit currently drops turn timing. Historical per-call generation speed is
not established by the checked rollout format.

## Cursor Agent CLI

Sources: [official output format][R1], [CLI changelog][R2],
[SDK usage surface][R3], installed CLI writer and bounded local reads.
This scope does not include IDE editor history or billing/admin queries.

- Verified parsed JSON blobs in `store.db` and conversation transcript JSONL
  lacked output-token usage and compatible model timing. Session metadata
  dates and tool-start markers do not supply a model-throughput denominator.
- Binary DAG per-blob coverage is **unverified**. Installed descriptors include
  optional assistant/thinking timing and separate live token-update types,
  but schemas/ASCII header scans do not establish which values are persisted
  in each binary blob. Do not convert this unknown into “no native data”.
- Live success stdout has conditional `result.usage`. Official docs and the
  inspected writer both show `duration_api_ms == duration_ms`: total execution
  time, not independently measured API latency. Captured stdout could support
  a run-average quotient, not historical recovery from the checked stores.
- `timestamp_ms` in partial output is a client arrival/flush observation.
  Hooks, SDKs and server usage endpoints are separate sources with their own
  versions/scopes; a dated forum report is not a current official contract.

Keep the existing unsupported historical usage mapping until a native mapping
is verified. Blob identity alone does not prove model-call identity.

## Verification and follow-up boundary

Workers performed web searches/page reads, native writer inspection and bounded
read-only native probes. No new test-agent execution, provider/billing query,
hook collector, runtime installation or history mutation was used for acceptance. SQLite was
queried through its native read-only CLI; no immutable assertion was made for
live WAL stores. Reads at different times can differ while writers are active.

Independent coordinator checks (counts only; no session content or ids published):

| Runtime | Independently checked evidence |
|---|---|
| Pi | Installed timer and two pinned upstream files; 416 assistant records in four recent files carried duration; append lag was nonzero, disproving append=end equivalence. |
| OpenCode | Runner/projection at the pinned commit; 200 recent token-bearing v2 rows in a read-only transaction had compatible terminal windows; provider metadata observed only response ids. |
| Kimi | Installed timing builder and step projection; six wire files contained one usage+timing step and three minimal error steps. This establishes shape, not prevalence. |
| Claude | Official SDK docs separately checked; bounded three-file reads matched 598 linked reply records to request ids with nonnegative intervals. No exact dispatch/first-token semantics inferred. |
| Codex | Writer clocks, first-output triggers, item lifecycle and analytics sink; three bounded rollout reads found nine response usage records and three turn duration/first-output pairs, no profile keys. |
| Cursor | Official output page, installed result printer, and 24+100 parsed JSON blobs across two stores; unsupported binary coverage remains explicit. |

The current checkout's `UsageObservation`/`UsageRecord` in
[`usage.py`](../src/sesskit/usage.py) have no typed timing/rate fields.
A native capability is **not** an implemented or released SessKit feature.

Recommended next implementation slice, not adopted API design: preserve the
supported per-operation native timings and their endpoint meanings through the
existing adapters before adding named derived rates. Reconcile usage duplicates,
scope, retries and finality with the same native feeds rather than creating a
second parser. A future code change needs the normal
[parser/load verification](CONTRACT.md#verification-mandatory-when-changing-parsers-or-load-wiring);
this research is not parser acceptance or a benchmark.

Evidence limits remain explicit: checked writer versions only; incomplete Pi/
OpenCode timing coverage; one successful local Kimi timing sample; closed Claude
writer semantics; undecoded Cursor binaries; no provider-exact decode validation.
These are declared capability boundaries, not silent passes.

## References

[P1]: https://github.com/earendil-works/pi/blob/6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9/packages/ai/src/types.ts#L585-L615
[P2]: https://github.com/earendil-works/pi/blob/6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9/packages/ai/src/utils/event-stream.ts#L91-L130
[P3]: https://github.com/earendil-works/pi/blob/6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9/packages/coding-agent/docs/session-format.md
[P4]: https://github.com/earendil-works/pi/blob/6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9/packages/coding-agent/docs/message-types.md
[O1]: https://github.com/sst/opencode/blob/69269e4ebe309a08919b3c9be7948b9fd699c3a4/packages/core/src/session/runner/step.ts
[O2]: https://github.com/sst/opencode/blob/69269e4ebe309a08919b3c9be7948b9fd699c3a4/packages/core/src/session/message-updater.ts
[O3]: https://github.com/sst/opencode/blob/69269e4ebe309a08919b3c9be7948b9fd699c3a4/packages/core/src/session/usage.ts
[O4]: https://github.com/sst/opencode/blob/69269e4ebe309a08919b3c9be7948b9fd699c3a4/packages/schema/src/session-message.ts
[O5]: https://opencode.ai/docs/cli
[K1]: https://moonshotai.github.io/kimi-code/en/configuration/data-locations.md
[K2]: https://github.com/MoonshotAI/kimi-code/blob/b12d13e53cf63ee37bc367793acaf6631312d4c3/packages/agent-core-v2/src/agent/usage/usageOps.ts
[C1]: https://code.claude.com/docs/en/agent-sdk/cost-tracking
[C2]: https://code.claude.com/docs/en/agent-sdk/typescript
[C3]: https://platform.claude.com/docs/en/build-with-claude/fast-mode
[X1]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/core/src/turn_timing.rs
[X2]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/core/src/state/session.rs
[X3]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/rollout/src/policy.rs
[X4]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/core/src/session/mod.rs#L5554-L5571
[X5]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/analytics/src/client.rs#L648-L652
[X6]: https://github.com/openai/codex/blob/67f0994b532b8740f14b5b738af7b7b2a915b1d6/codex-rs/exec/src/event_processor_with_jsonl_output.rs
[X7]: https://developers.openai.com/codex/non-interactive-mode
[R1]: https://cursor.com/docs/cli/reference/output-format
[R2]: https://cursor.com/docs/cli/changelog
[R3]: https://cursor.com/docs/sdk/python
