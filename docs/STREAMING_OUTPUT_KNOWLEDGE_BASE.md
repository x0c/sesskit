# Agent output streaming knowledge base

Research checked 2026-11-02–03. Scope: whether SessKit can read agent output as it
is produced, including sessions launched elsewhere. The historical study below
is evidence, not an implementation claim.

## Delivery requirement

Owner requirement (2026-11-03): third-party programs must be able to display agent
messages incrementally through SessKit. Implementation is now authorized, replacing
the earlier research-only boundary for this outcome. SessKit owns normalized message
observations; consumers own presentation and execution owners keep process control.

Reuse native session/message identities and existing history interpretation. Expose
real partial text when the selected source provides it; explicitly identify
persisted-record delivery otherwise. Automatic service degradation is required:
when a live source is unavailable or disconnects, continue from persisted history
where available instead of blocking the consumer or asking for fallback approval.
Expose the current delivery mode, degradation reason and unrecoverable fragment
gaps; reconcile completed messages without replaying duplicate text. Closing a
consumer stops observation, not execution. Deliver machine-readable stream records with
initial state, ordering, updates, completion/error and gap/reset semantics. A
consumer must not double-append native final-message replays, mistake disconnect
for successful completion, or abort the agent merely by closing its reader.
Incremental token/cost accounting (U4) and new UI remain outside this change.
The additive implementation is documented in the [message streaming guide](MESSAGE_STREAMING_GUIDE.md).
It normalizes host-provided native events and automatically follows existing
history; it does not create native transport/control infrastructure. Current source
acceptance covers format fixtures and actual local-history consumption separately
from the unperformed shared-server live attachment experiments below.

## Conclusion

**Yes: native structured live-output paths exist, but there is no universal tap
for every independently running terminal agent.** Shared-server subscriptions and
launcher/host-provided event feeds are different integration paths. Observation
does not inherently require owning execution.

Existing SessKit readers follow persisted records for Claude, Codex, OpenCode,
Cursor and Pi. Kimi currently has snapshot parsing/refresh, not an incremental
activity reader. None of those declarations promises access to text fragments
before the native writer persists them. Incremental usage accounting (U4) remains
a separate, unimplemented capability.

Reusable ownership, granularity, detach and replay distinctions live in the
[shared session-integration guide](~/.config/agentsync/docs/AGENT_SESSION_INTEGRATION_GUIDE.md#live-output-observation-and-history-following).
This document owns SessKit mappings, dated native evidence and proposed next checks.

## Evidence levels

- **Observed:** bounded native-history reads or exercised public reader behavior.
- **Source/documentation:** an inspected implementation or current official
  documentation supports the behavior; no live model turn was executed.
- **Unknown:** the inspected evidence does not resolve that question. Missing
  rows, undecoded binary stores and an unexercised attach path are not proof of
  impossibility.

All six research workers used OpenCode Muse Spark 1.3 Contributor (xhigh). The
coordinator checked reports against native source and official references,
corrected owner-versus-observer overclaims, and rechecked pinned source identities.
**Beyond the authorized research workers, no runtime/model execution was launched
as a streaming probe; no server launch, cancellation, hook installation or runtime
configuration change was used for these checks.** Existing native
records and synthetic fixtures are not fresh live-stream acceptance.

## Runtime feasibility

| Runtime / inspected version | Native live-output path | Session already running elsewhere | Replay and strongest current evidence |
|---|---|---|---|
| OpenCode 2.2.5; matching source tag | Existing server SSE `event.subscribe`; text/reasoning/tool-input/progress events | Independent authenticated connection to the same reachable server; no new executor needed | Fragments are volatile. Source exposes durable per-session `log` with `after`/`follow` for committed events, not lost deltas. Installed-store shapes observed; live subscription/replay not exercised. [O1] [O2] |
| Codex CLI 0.166.0; newer source 18c6dbe5 | App-server notifications such as `item/agentMessage/delta` | Another accepted connection can rejoin a loaded, running thread on the **same** listener-enabled app-server. Default private embedded TUI is different; a second private server does not expose its live turn | Loaded-thread resume sends history and atomically subscribes to new updates. Historical rollout/exec JSON suppress deltas in inspected source. Source/doc evidence; installed-build live rejoin and version equivalence not established. [X1] [X2] [X3] |
| Kimi Code 2.4.1; matching package source | Existing `kimi web` WebSocket; ACP or print paths under their owning host | Passive authenticated subscriber to an existing web-owned session is distinct from a competing writer. A second writing process may receive `SESSION_OCCUPIED`/40931 | Durable transcript sequence/catch-up and volatile delta frames differ. Pinned implementation handles `subscribe_v2`; the documentation walkthrough's older `subscribe` spelling conflicts. Source/doc feasibility only; live protocol test required. [K1] [K2] |
| Pi coding agent 1.4.0 docs; newer source d6af4d18 | SDK `session.subscribe`, in-process extension, JSON/RPC output of an existing owning host | No passive cross-process attach endpoint found in inspected native RPC surface. An SDK/extension listener inside the existing host can observe without taking execution ownership; uninspected brokers are not ruled out | Persisted messages are written on `message_end`, not each delta. SDK unsubscribe removes one listener; abort/dispose and owned RPC teardown are separate. Public history-reader following observed; live emission source/doc only. [P1] [P2] |
| Claude Code 2.1.316; closed writer | CLI `-p --output-format stream-json --verbose --include-partial-messages` or Agent SDK partial-message feed | Documented paths observe their owned run. `agents --json`/recent logs are snapshots and interactive `attach` is not proof of a passive structured delta feed | Partial `stream_event` delivery documented. Existing JSONL rows/readers observed; fresh live emission, reasoning-delta forwarding, replay and backpressure unverified. [C1] [C2] |
| Cursor Agent CLI 2026.10.22-27dcf5f; closed bundle | Print `stream-json` **plus** `--stream-partial-output`; ACP/SDK feeds where supported | Existing independent CLI observation not established. `persist attach`, ACP load and SDK resume need their own live-semantics checks; do not label them impossible from names/help alone | Without partial output, stdout aggregates complete messages. Partial output also emits duplicate flushes. Decoded JSON stores observed; binary payloads remain undecoded, so global counter/timing absence is not established. [R1] [R2] [R3] |

### OpenCode: native attachment remains a separate host boundary

The inspected bus distinguishes a volatile subscription from its durable log.
SSE subscriber capacity is 4096; overflow fails/removes that subscriber. Its
release finalizer removes the subscriber and closes its queue, not the execution.
Authentication and server/location scope still apply.

Use committed history/log records to reconcile after disconnect. A completed
message can recover final text, but it cannot recreate the original intermediate
fragments or their arrival clocks. The sampled local event table held legacy v1
rows; v2 `session.log` replay effectiveness on this machine remains unverified.
`opencode run --format json` is not equivalent to the raw delta subscription:
the inspected CLI emits complete events. [O1] [O2] [O3]

### Codex: shared server is different from a private terminal

Pinned source explicitly supports a running-thread resume response that sends
history and atomically subscribes the joining connection. Event handling fans out
to subscribed connection IDs. Disconnect/unsubscribe removes a connection;
no-subscriber unloading in the checked source defers while the thread is running.
These are source findings, not a live test of CLI 0.166.0.

A future passive adapter must not send turn/start, steer, interrupt or configuration
overrides. Check the actual build's protocol before treating resume/join as a
read-only observer operation. WebSocket transport is marked experimental and
unsupported for production by the current official guide; reachable does not mean
authenticated, stable or safe to expose publicly. [X1] [X2] [X3]

### Kimi and Pi: detach is not abort

Kimi's inspected WebSocket close handler clears timers/queues and unsubscribes the
client; writing holds and explicit cancellation are separate paths. The stale
documentation frame spelling must be resolved against the served protocol before
implementation. Pi's SDK unsubscribe removes only its registered listener; disposing
the session or closing/terminating an owned RPC process is a different action.
Neither finding authorizes taking over somebody else's session file. [K2] [P1]

### Cursor: partial chunks and duplicate flushes

The current official output contract distinguishes three assistant-event shapes:

| `timestamp_ms` | `model_call_id` | Interpretation in partial-output mode |
|---|---|---|
| Present | Absent | New text chunk; append exposed text |
| Present | Present | Buffered pre-tool flush; duplicate, do not append again |
| Absent | Absent | Final flush; duplicate, do not append again |

A successful stream ends with `result`; errors can end earlier with nonzero exit
and stderr, without any terminal result event. EOF alone is not success. This
deduplication rule is Cursor/version-specific, not a generic event protocol.
Reasoning text in stored history does not establish reasoning-fragment forwarding
in print mode. [R1]

## Independently checked evidence and limits

- Official headless/streaming docs were read for Claude and Cursor. The coordinator
  rechecked Cursor's partial-output/dedup table and Claude's required flags.
- Official Codex app-server docs and pinned source confirm per-connection
  subscriptions, loaded-thread rejoin, fan-out and deferred unload. Four cloned
  source identities below were obtained with `git rev-parse HEAD`; representative
  official raw files were fetched and byte-compared with their local checkout.
- OpenCode 2.2.5 package metadata matches the inspected tag. Pinned bus and SSE-feed
  code establish volatility, queue overflow and detach cleanup. Native SQLite CLI
  reads were read-only and transaction-scoped; source-only claims remain separate.
- Kimi's bounded six-file sample contains successful and failed durable turn markers
  but no persisted delta records. Its source durability flags, not that small
  absence count, support the live-versus-journal distinction.
- The coordinator independently exercised Pi's public `open_activity_reader`:
  cold read 1 event, newline-less appended assistant line 0 new events, newline
  completion 1 new event, cursor-resumed reader 0 repeated events, same generation.
  This proves complete-record following, not token streaming.
- Native history samples support store interpretation only. Flush/arrival latency,
  fresh success/error/abort streams, second-client live attach, real overflow and
  cross-version replay have **not** been behaviorally accepted.

Private reports, source clones, shape-only evidence, correction/acceptance records
and exact native locators are retained at
`~/.config/sesskit/development/streaming-research-20261102-6dda6090/`.
Do not publish those native stores or conversation payloads. This knowledge base
contains only sanitized findings.

## Future native transport checks

1. **OpenCode existing-server observer:** bounded authenticated subscription during
   a legitimately running disposable turn; count native delta/lifecycle events,
   disconnect the observer without an interrupt, verify the run continues, then
   reconcile final state and declare any missing fragments. Do not start or expose
   a new service merely to make attachment appear supported.
2. **Codex and Kimi shared-server observers:** test a second client against the
   exact served build/protocol; distinguish initial snapshot from live delivery,
   check credentials, identity, replay/gap handling and detach-only behavior.
3. **Host-provided feeds:** verify Claude/Cursor partial-output flags and Pi
   SDK/extension events when the existing execution owner exposes them. Do not
   create another launcher/parser hierarchy in SessKit or retrofit a private
   running process by silently restarting it.
4. These transport experiments remain distinct from the subsequently authorized
   host-provided observation API. The current message stream uses existing
   session/message/turn/evidence abstractions and advertises no automatic passive
   attachment to a private process. The earlier research report itself did not
   authorize implementation; the later Delivery requirement above supersedes that
   research-only boundary.

A separate incidental Kimi finding remains to verify: native failed-turn markers
can exist without assistant text while the legacy status path uses tail roles.
Record it as parser follow-up, not as the answer to the streaming request or an
authorization to change status semantics in this task.

## Inspected source pins

| Source | Commit | Relationship to installed runtime |
|---|---|---|
| anomalyco/opencode | `9bbb8ce18c80f099e0e448775f59d965ead40a21` | v2.2.5 matches installed package metadata |
| openai/codex | `18c6dbe52b3b7cef89c13d52efa535ce39bbe27b` | Newer reference snapshot; CLI 0.166.0 equivalence unverified |
| MoonshotAI/kimi-code | `fc3b521b8b68468268bb4cfe7f45008ce08b2b99` | CLI package 2.4.1 matches installed metadata |
| earendil-works/pi | `d6af4d18e55b86ddef249d5bf8a4939601707945` | Newer reference snapshot; installed 1.4.0 docs cross-checked |

## References

[O1]: https://github.com/anomalyco/opencode/blob/9bbb8ce18c80f099e0e448775f59d965ead40a21/packages/core/src/bus.ts#L126-L165
[O2]: https://github.com/anomalyco/opencode/blob/9bbb8ce18c80f099e0e448775f59d965ead40a21/packages/server/src/event-feed.ts#L8-L83
[O3]: https://opencode.ai/v2/docs/api
[X1]: https://learn.chatgpt.com/docs/app-server
[X2]: https://github.com/openai/codex/blob/18c6dbe52b3b7cef89c13d52efa535ce39bbe27b/codex-rs/app-server/src/thread_state.rs#L65-L71
[X3]: https://github.com/openai/codex/blob/18c6dbe52b3b7cef89c13d52efa535ce39bbe27b/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L567-L622
[K1]: https://www.kimi.com/code/docs/en/kimi-code-cli/reference/server-api.html
[K2]: https://github.com/MoonshotAI/kimi-code/blob/fc3b521b8b68468268bb4cfe7f45008ce08b2b99/packages/kap-server/src/transport/ws/v1/wsConnectionV1.ts#L196-L346
[P1]: https://github.com/earendil-works/pi/blob/d6af4d18e55b86ddef249d5bf8a4939601707945/packages/coding-agent/src/core/agent-session.ts#L1404-L1449
[P2]: https://github.com/earendil-works/pi/blob/d6af4d18e55b86ddef249d5bf8a4939601707945/packages/coding-agent/src/core/agent-session.ts#L1159-L1182
[C1]: https://code.claude.com/docs/en/headless
[C2]: https://code.claude.com/docs/en/agent-sdk/streaming-output
[R1]: https://cursor.com/docs/cli/reference/output-format
[R2]: https://cursor.com/docs/cli/acp
[R3]: https://cursor.com/docs/sdk/python
