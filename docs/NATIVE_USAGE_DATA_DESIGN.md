# Native usage data design

Status: owner-approved product boundary; proposed technical contract, ready for
implementation planning. No API, command, schema, or parser change in this
document is implemented by this design-only delivery.

## 1. Purpose and boundary

SessKit supplies useful, trustworthy native session data. It does **not** run
agent evaluations, define benchmark tasks, judge correctness, or calculate
success scores. Consumers must not need a second native-history parser to
obtain model usage and its association with messages, turns, and sessions.

The requirement is to expose the finest granularity actually supported by the
source, preserve coarser evidence when finer attribution is impossible, and make
missing data, overlap, and uncertainty machine-readable. "As much useful data as
possible" does not authorize collecting credentials, executing agents, querying
billing accounts, or inventing measurements.

In scope:

- Native token counters, recorded monetary amounts and their calculation basis.
- Model-call identity and links to native messages, turns, tools, branches,
  auxiliary operations, and related sessions where supported.
- Reconciliation and arithmetic summaries over explicitly selected native data.
- Model/provider/configuration and timing evidence useful for interpreting usage.
- A versioned, language-neutral export, not Python-only dataclass serialization.

Out of scope: task creation or task inference, benchmark/attempt records,
scoring, quality judgments, prices fetched from the network, tokenizer estimates,
subscription allowance calculations, invoicing, agent execution, and a telemetry
collector/service. A native `task_complete` marker proves neither correctness
nor that a consumer-defined task is complete. An external task may span several
turns or sessions; its consumer selects the relevant native identities.

General evidence rules remain in the shared
[recorded usage guide](~/.config/agentsync/docs/AGENT_SESSION_INTEGRATION_GUIDE.md#recorded-usage-and-estimates).
This document owns SessKit's detailed mapping and proposed interfaces.

## 2. Research and current baseline

Research checked on 2026-10-30. Protocol/SDK documentation describes those
surfaces, not automatically their persisted history. Every adapter mapping must
also have native-format fixtures and source-version evidence.

### 2.1 Native sources

| Runtime | Useful native evidence | Interpretation and remaining verification |
|---|---|---|
| Claude Code | Assistant `message.id`, `message.usage`, model, request/parent linkage where recorded; `cost-state`/per-model rollups in observed history variants | Deduplicate a model response split into multiple messages. The SDK explicitly warns that per-step output counters can be placeholders, while result `modelUsage`/cost can include earlier resumed-session spend, subagents and auxiliary calls. Do not transfer SDK finality assumptions blindly to every JSONL version. Preserve per-field quality and the rollup's actual scope. [C1] |
| Codex | `token_usage_record.payload.usage` with `response_id`; `turn_token_usage`, `thread_token_usage`, turn/root-turn/thread ids; legacy `event_msg/token_count.info.last_token_usage` and `total_token_usage`; model context and compaction linkage | Separate request values from turn/thread counters. New and legacy records can describe the same request. Counts alone or adjacency alone do not prove identity. Keep unlinked legacy observations when a request-level association cannot be established. Official `exec --json` also returns turn usage, but a live JSON stream is not a stored-history contract. [X1], [X2] |
| OpenCode | Assistant message `tokens`, recorded cost, model/provider, retry and created/streamed/completed timestamps; step/usage events and session aggregates in supported store versions | The inspected upstream mapping stores **non-cached input** and **visible output** separately from cache and reasoning. Step/message values and session aggregates must not both enter the same sum. Verify each supported v1/v2 store layout; newer event shapes do not imply an installed database migration. [O1] |
| Pi | Assistant `usage`, provider/API/model/response ids, requested and reported model/reasoning metadata; tool-result `usage`; independent `usage` entries; compaction and branch-summary usage; tree/fork/context-edit metadata | Tool-only assistant responses still consume tokens. Tool-result usage is nested model work, not a second main-loop response. Unknown independent usage kinds are valid. `reasoning` is already included in `output`; one-hour cache writes are a subset of cache writes. Context edits change future context, not past accounting. [P1], [P2] |
| Cursor | The inspected official CLI output documents model-call ids, terminal results and timing; the currently supported local-store interpreter has no verified message-usage mapping | Keep usage unsupported until a native source is demonstrated. Billing dashboards or an undocumented private endpoint are not substitutes for local message attribution. Documented `duration_api_ms` currently equals total duration; it is not independently measured model latency. [R1] |
| Kimi | Existing legacy history support; typed usage adaptation is deferred | Preserve that boundary. This design does not enable a new adapter or claim usage coverage. |

### 2.2 Reference implementations and what is reused

[ccusage at `71e022a8`](https://github.com/ccusage/ccusage/tree/71e022a8edf98b41b5715b77b17b3ea5a166498b)
was inspected from a shallow clone outside the workspace:

- `rust/adapters/codex/src/parser.rs`: cumulative-baseline handling, repeated
  token-count observations, and compaction response-id correlation.
- `rust/adapters/opencode/src/parser.rs` and `loader.rs`: message-level versus
  session-aggregate overlap, including the regression that excludes aggregates
  when message records exist.
- `rust/adapters/pi/src/parser.rs`: native message usage, dedup identities, and
  the limitation of an assistant-message-only filter relative to current Pi.

Reuse the adapter-local mapping, correlation and adversarial fixture patterns.
Do **not** copy implementation, pricing lookup, zero-record omission, fallback
model guesses, counter clamping, or a report policy that discards conflicting
native aggregates. SessKit preserves evidence rather than choosing display
convenience over data fidelity. No source code was copied for this design.

[Langfuse usage tracking][L1] separates ingested usage/cost from inference and
records usage per model generation. Reuse the distinction and call-first
aggregation, not its service, evaluation features, tokenizer or price inference.
[OpenTelemetry GenAI attributes][T1] define inclusive input/output totals with
cache and reasoning subsets. Reuse that arithmetic vocabulary, not the telemetry
schema or dependency; the cited attributes are still marked Development.

### 2.3 SessKit gaps demonstrated by source and local history

Current `Usage` (`src/sesskit/models.py`) stores native scalar fields and evidence
on activity events. `examples/usage_and_outcomes.py` intentionally does not sum
them. This is a useful compatibility view, not an additive accounting dataset.

Confirmed source-level gaps:

1. `_CodexFeed.feed` keeps the first `turn_token_usage` with `setdefault` rather
   than retaining request usage and subsequent turn-counter revisions. A native
   response id is not currently the accounting identity.
2. `_build_pi_events` attaches usage to text/error events but not ordinary
   tool-only assistant responses. Its message-first filtering cannot represent
   every independent usage, branch-summary or nested tool-use record.
3. `_pi_usage` does not expose the observed `reasoning` field. The common model
   lacks provider, counter semantics, currency, cost basis and finality.
4. Existing incremental activity results do not re-emit late usage enrichment.
   An accounting consumer cannot rely on appended activity events alone.
5. Transcript v1 omits usage; `asdict()` recipes are not a versioned external
   usage contract. A coarse `usage_data` capability does not describe granularity
   or completeness.

A bounded, read-only sample of the five most recently modified JSONL files per
runtime found: Claude 5 assistant usage records; Codex 8 request records and
9 token-count observations, including two turns whose final cumulative total
exceeded their first; Pi 557 assistant usage records, 480 without text, plus
2 compaction entries with usage. These counts establish concrete shapes and
counterexamples only, not runtime-wide prevalence or complete coverage. Files
may have been live; no prompts, paths, ids or native content were exported.
OpenCode/Cursor findings here are source/documentation checks, not new database
probes or live agent executions.

## 3. Data model

Use three layers. An observation is evidence, a record is one reconciled unit,
and a summary is a scoped arithmetic projection. None is an evaluation result.
Names below are proposed, not current public symbols.

Granularity is explicit: a **call** is one model request/response operation; a
**message** is a native message, not each rendered text/thinking/tool fragment;
a **turn** is one interaction span that may contain multiple calls and tools;
a **session** is a native history unit. An OpenCode step is not automatically a
user turn. Prefer native turn boundaries; existing deterministic turn grouping
may be reused with inferred evidence, never relabeled as a native id or used to
hide ambiguous association. A consumer-defined task is none of these by default.

### Common abstraction first

Follow the [general-purpose abstraction boundary](ABSTRACTION_REDESIGN.md#native-data-scope-owner-clarification-2026-10-30).
Usage is a view of shared history entities, not a second session/turn/message tree.

| Common concept | Integration rule |
|---|---|
| Session, turn, native message and activity identity | Reuse the existing canonical identities and normalized turn grouping; do not introduce usage-specific copies. Keep native ids separate from normalized ids and preserve their evidence. |
| Model call | Add only the missing minimal shared call identity/metadata if needed. A call can exist without a visible message and can link to multiple messages; several calls can contribute to one interaction. This is historical data, not a second executor. |
| Tool and auxiliary work | Link existing tool-invocation/activity identities. Compaction, cache warming and nested work must not be fabricated as assistant text or forced into a tool-call class. |
| Relations and evidence | Reuse `Evidence.origin` (`native`, `inferred`, `unknown`) and the common relation contracts. Session lineage stays in the existing session-relation model; usage does not invent another parent tree. |
| Metrics | Typed token metrics, monetary amounts and their evidence are reusable value types. Additional request/count/modality measurements retain explicit family and unit, rather than being forced into `total_tokens`. |
| Runtime differences | Adapters map native records to these concepts. Core reconciliation/selection/serialization consumes normalized scope, counter and inclusion semantics, never branches on a runtime name. |
| Extensions | Keep unmapped native fields under an adapter/version namespace with evidence and the export-safety limits below. Extensions cannot redefine a canonical metric or replace typed core fields with arbitrary dictionaries. |

Model/provider/configuration is attributed to the operation that produced the
data, not a single session-wide model. Until the planned `SessionRef` split
lands, reuse the existing runtime/session-id identity rather than requiring that
unrelated migration. Reuse `Turn.turn_id` and activity message/call ids for links.
Metric finality and coverage are field- and scope-specific, not a fixed support
matrix baked into consumer code.
Generic summary algebra operates on declared scopes/inclusion relations; any
native counter quirk or migration rule stays in the adapter. Consumers select
capabilities and available evidence, not hard-coded runtime lists.

Generality acceptance includes a source with only a session counter, multiple
models in one session, one response split across messages, a call with no text,
a non-token metric and an auxiliary operation with no main-loop turn. All must
fit the same core without runtime-named fields, new host policy, or fabricated
fine-grained ids. This does not authorize new runtimes or an unrelated model
rewrite. Any new common type needs a concrete native mapping and compatibility
tests rather than speculative universal workflow abstractions.

### 3.1 `UsageObservation`: preserve what the source said

| Field group | Required meaning |
|---|---|
| Identity | `observation_id`, runtime/session ref, source generation, record/field locator; no prompt/content hash as a call identity |
| Entity | `scope_kind`: `call`, `message`, `turn`, `session`, `unknown`; native scope id where present |
| Measurement | `counter_kind`: `absolute`, `delta`, `cumulative`, `unknown`; cumulative counter identity and reset epoch only where supported |
| Operation | `model_call`, `compaction`, `branch_summary`, `cache_warm`, `tool_nested`, or `unknown`; preserve the native operation string for unfamiliar kinds |
| Correlation | Native response/request/message/turn/tool ids, branch entry, root turn, parent/origin session and their individual evidence; each optional |
| Attribution | Requested model, reported/actual model, provider/API, requested/reported reasoning level, service tier and safe native configuration where present; never replace a request's model using a later selection |
| Values | Native metric names and numeric values, native monetary amounts, units, inclusion relationships and per-field quality; absent differs from zero |
| Scope coverage | Whether child sessions, auxiliary calls or inherited history are included: `true`, `false`, or unknown; aggregate counters can cover more than one local session |
| Time | Recorded timestamp plus its meaning; normalized UTC timestamp and duration unit; start/end/duration only if natively recorded or explicitly derived from compatible endpoints |
| Evidence | Existing `Evidence` references/origin, adapter mapping/version, source variant, metric calculation basis and diagnostics |

A snapshot's native numeric values remain available even if rejected by normalized
validation. Booleans, negatives, fractional token counts, non-finite values and
malformed structures get diagnostics; do not round, clamp, coerce to zero, or
silently drop their containing operation. Invalid numeric lexemes use tagged
raw representations in JSON, never JSON `NaN`/`Infinity`. Native cost precision
is preserved; normalized money uses finite decimal strings and an optional
currency, not binary-float accumulation.

### 3.2 `UsageRecord`: one reconciled unit, not one displayed event

A record has a stable `usage_id`, source observation ids, the entity/operation
and links above, normalized metrics, `revision`, and reconciliation diagnostics.
Records can be call-level details **or** coarser rollups; record lists are not
universally additive. Summaries return the selected contributing record ids.

- Prefer native response/entry/message identity within its runtime and proven
  origin. Otherwise use a generation-local source-position identity, explicitly
  labeled `identity_basis=source_position`. Do not promise rewrite-stable ids.
- Response splitting, text/thinking/tool fragments and repeated snapshots refer
  to the same usage record; they do not create new consumption.
- Expose message-to-usage links as many-to-many. Keep only a turn/session link
  when a finer native link is absent; do not divide a turn's tokens among messages.
- A user message's text-token size is not the cost of processing that message.
  This design does not add a tokenizer or pretend each user/tool message has an
  independently billed token count.
- Link a revision to the same usage id. A source correction is an update, not a
  second amount to add. Conflicting final observations remain inspectable.

Per metric retain `value`, `basis` (`native` or exact `derived` arithmetic),
`quality` (`final`, `provisional`, `placeholder`, `unknown`), evidence references,
and a named normalization rule when derived. Exact arithmetic uses the existing
inferred-evidence origin plus that explicit rule and native inputs; it does not
introduce a competing `Evidence` enum. A record-wide native marker cannot make
a placeholder output count final. A terminal assistant message also does
not prove that an initialized zero was provider-measured.

### 3.3 Normalized tokens and amounts

The new dataset uses explicit inclusive names; **do not change legacy `Usage`
field meanings**:

| New metric | Meaning |
|---|---|
| `input_tokens_total` | All processed input tokens, including supported cache subsets |
| `input_tokens_uncached` | Input not included in cache-read/write buckets, when known |
| `cache_read_input_tokens` | Input served from cache; a subset of total input |
| `cache_write_input_tokens` | Input written to cache; a subset of total input |
| `token_breakdowns` | Typed subdivisions by modality or cache-retention seconds, with explicit parent metrics; never independent addends |
| `output_tokens_total` | All output tokens including reasoning where source semantics establish that relationship |
| `reasoning_output_tokens` | Reasoning subset of total output |
| `total_tokens` | Inclusive input plus output, when comparable and available |

`token_breakdowns` uses typed dimensions, such as `modality` and
`cache_retention_seconds`, rather than a new core field for every provider's
cache tier. For example, native Pi `cacheWrite1h` maps to a cache-write breakdown
with retention 3600 seconds, included in `cache_write_input_tokens`. Unknown
native subdivisions stay namespaced until their unit/inclusion mapping is known.

Verified mappings are variant-specific:

- Anthropic: total input is `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens`, not `input_tokens` alone. Missing required
  addends remain unknown unless the source contract explicitly defines omission
  as zero. Preserve cache-retention details, server-tool usage and other native
  usage breakdowns. [A1]
- Codex: cached input is a subset of reported input. Preserve request `usage`,
  turn and thread totals separately; do not add cache or reasoning to an already
  inclusive total. Verify legacy `last_usage_includes_reasoning` variants rather
  than applying one formula to every log.
- Pi: preserve native total, cache and reasoning. `reasoning` is already inside
  output. The inspected session-stat/cost code treats `input`, `cacheRead` and
  `cacheWrite` as separate addends, so inclusive input is their sum for that
  normalized schema. Unsupported legacy/custom provider variants keep their
  native fields without an unverified formula. [P2], [P3]
- Inspected OpenCode mapping: total input is non-cached input plus cache read
  and write; total output is visible output plus reasoning. Apply only to the
  verified mapping/store variant, not to every object named `tokens`. [O1]

Keep reported totals beside calculated totals. A disagreement is data, not a
reason to overwrite one. Native billed-unit counters and model-consumed counters,
if both exist, remain distinct metric families with their original units.
Unknown multimodal or credit/request metrics survive as namespaced extensions;
never treat them as interchangeable tokens or money.

Recorded monetary values carry `currency`, `calculation_basis`
(`provider_reported`, `runtime_estimate`, `unknown`) and native price-table/tier
metadata if supplied. Basis is per amount: current Pi's `cost.totalFromProvider`
can mark a provider-reported total while its component costs remain catalog
estimates. Their unequal sum is not a same-basis accounting conflict. An absent
marker means estimate only for a verified schema that defines that default.
OpenCode can select endpoint cost or a catalog fallback before persistence;
a stored `cost` without discriminating evidence keeps basis unknown. [O1], [P3]

Persisted does not mean invoice-authoritative. Preserve recorded zero costs.
Do not fill missing cost from prices, convert currencies, or sum unknown
currencies together. A subscription user can have token usage without a
meaningful pay-per-call charge. [C1], [L1]

## 4. Reconciliation and summaries

### 4.1 Identity and counters

1. Collect usage before filtering empty text, hidden messages, error-only output,
   active branches or model-context compaction. Keep auxiliary and usage-only rows.
2. Correlate using native identity and established scope. Same numbers, adjacent
   storage, timestamp proximity or identical text alone never prove duplication.
3. Preserve duplicate-source observations but reconcile proven duplicates to one
   usage id. Legacy records without a proven link remain ambiguous, not extra
   additive calls beside their possible replacements.
4. Treat repeated cumulative observations as revisions of one counter in one
   epoch. Select the latest valid observation according to the source's update
   semantics, not the first, the numerical maximum, or the sum. A final source
   correction can decrease a value; preserve it and its provenance.
5. Derive a counter delta only between compatible observations with the same
   counter scope, model dimension, inclusion semantics and epoch. Never assume
   an unknown starting baseline is zero. A decrease without a proven correction
   or reset creates a diagnostic; it is not silently clamped to zero.
6. An exact counter delta may remain an unattributed interval/turn record. It
   must not become an invented per-message call. Do not add such a delta to the
   detailed calls already covered by that interval.
7. Reconcile request details with matching turn/session rollups per metric and
   declared scope. Preserve overlaps, unexplained differences and residuals.

### 4.2 `UsageSummary`: scope and coverage are part of the answer

Every summary states selected session refs/usage ids, history view, source
watermark/generation, model/provider grouping, self-versus-descendant scope,
included operation categories, read coverage and unresolved sources.

Per metric return:

- `known_total`: the sum of a **proven non-overlapping** selected subset, or null
  if none is defensible; never an arbitrary sum of all observations.
- `complete_total`: a final total only when coverage is established for that
  explicitly declared native scope; otherwise null.
- Native `reported_totals`, calculated `detail_total`, selected record ids,
  unattributed amounts where derivable, and reconciliation diagnostics.
- Coverage: `complete`, `partial`, `unknown`, `conflicted`, `not_recorded`, or
  `unsupported`, plus counts of observed/selected/unattributed/rejected records.
  Do not invent a percentage when the true number of calls is unknown.

A final native session rollup can establish a scoped total even when individual
message attribution is partial. Prefer that rollup for the matching metric/scope,
not for a narrower scope it includes only implicitly. Do not add the rollup to
its child details. If supposedly complete details disagree with a comparable
final rollup, expose both as conflicted and leave `complete_total` null.

"Complete" is relative to readable native history and the declared inclusion
contract, never a guarantee of all provider-billed work. Missing files, unsupported
operation variants, unresolved duplicate sources or truncated reads prevent a
whole-scope completeness claim. A readable source with no recorded usage reports
`not_recorded`, not a zero-cost session. Explicit native zero remains zero.

### 4.3 Branches, children, copies and external selections

- Default session accounting uses `history_view=all_branches` and the session's
  own operations. `active_branch` is an explicit alternate view. Both include
  past calls before compaction; neither means only the current model context.
- A branch switch does not undo past consumption. A copied/inherited prefix in
  a fork is provenance, not a new call. Deduplicate shared origin only with
  supported lineage/identity evidence; equal content is insufficient.
- Related sessions remain separate by default. Descendant summaries require an
  explicit finite set of available snapshots and native relation evidence.
  Detect cycles, missing children and unknown ancestry; never silently scan an
  unlimited session forest or treat missing child history as zero.
- A parent rollup may already include child or auxiliary usage. It cannot be
  added to those details, nor labeled a self-only total without disaggregation.
  Unproven overlap leaves the combined result incomplete.
- Consumers can select native turn/call/session ids to represent an external
  task. SessKit does not create, infer or store the consumer's task, benchmark,
  attempt, completion criterion or score.

Synthetic acceptance examples (not values from local histories):

| Evidence | Correct answer |
|---|---|
| One response id on a thinking event and two text/tool fragments | One call, three links; count its usage once |
| Turn cumulative totals 100, 240, 310 in one epoch | Latest known turn total 310, not 100 or 650; finality still requires evidence |
| Request usage 100 plus its legacy duplicate 100 plus turn rollup 100 | Three observations, one consumption of 100 if linkage is proven |
| Native session total 500; verified subset details total 350 | Scope total 500 if final, detail attribution partial; derive 150 unattributed only if subset compatibility is proven |
| Complete comparable details 350; final rollup 500 | Conflict, not an automatically invented extra call of 150 |
| Anthropic uncached input 100, cache read 900, cache write 200 | Inclusive input 1200 |
| Inclusive output 80 with reasoning subset 60 | Total output 80, not 140 |
| Cache writes 200 with a one-hour breakdown 120 | Cache writes remain 200; represent 120 with retention 3600, not a runtime-specific core field or another addend |
| Verified OpenCode visible output 20 and separate reasoning 60 | Inclusive output 80 |
| Explicit recorded monetary zero; missing monetary value | Zero and null respectively; no price-based replacement |
| Fork copies a call, then creates one new call | Inherited call is not a new spend; cross-session union counts shared origin once only with lineage proof |

## 5. Other useful data alongside usage

Expose source-supported interpretation context through typed records/links, not
an unbounded dump of native payloads:

| Data | Useful meaning | Boundary |
|---|---|---|
| Requested/reported model, provider/API, reasoning level, service tier, context limit and native sampling configuration | Distinguish different model execution conditions and explain counters | No inference from aliases/current UI selection; never export auth/config files |
| Native request/response ids, turn boundaries, stop reason, error kind/code, retry/attempt linkage | Trace which operation produced a result or failed | Completion is not task correctness; only explicit retry evidence establishes a retry |
| Call/tool start/end, response-stream end, native durations | Attribute recorded time to operations | Preserve endpoint meaning; no guessed TTFT or model latency from message gaps; parallel durations are not additive wall time |
| Subagent/fork/continuation relations and compaction/context-edit markers | Explain changed context and nested work | History accounting differs from the context currently sent to a model |
| Safe native usage breakdowns, server-tool/request counters, attachment kind/count and references | Preserve information not captured by scalar tokens | Only native values; no automatic file reads/uploads or provider-billing queries |
| Human/injected/system origin, existing tool result and interaction evidence | Let consumers choose their own analytical scope | Keep existing ownership and unknown-evidence rules; no new product policy |

This is a data-preservation inventory, not authorization to rewrite every
existing abstraction. U1-U3 implement usage and its necessary attribution first;
common identity/evidence/value types stay in the shared model layer, while
usage-specific reconciliation stays in its owning module. Other concepts reuse
current typed structures or remain explicitly unmapped
extensions until their native mapping and compatibility tests exist.

## 6. Proposed API, export and compatibility

### 6.1 Python and parser integration

Proposed entry points:

```text
load_usage(session, *, history_view="all_branches", include_native=False)
    -> UsageSnapshot
summarize_usage(snapshot_or_snapshots, *, selection=None)
    -> UsageSummary
open_usage_reader(session, *, history_view="all_branches", cursor=None)
    -> UsageReader  # later U4, capability-gated
```

`UsageSnapshot` distinguishes `available`, `empty`, `unavailable`, `unsupported`,
contains observations/records/summary and source watermarks, and reports native
read coverage independently of conversational event count. It is a projection
container over shared history identities, not a new session hierarchy. A usage-only history
can be available even when its conversation is empty. A readable history without
usage is empty with `not_recorded` coverage. Failure is never empty success.
JSONL snapshots consume a frozen complete-record prefix; SQLite snapshots use
a consistent read transaction. Report that watermark and any replacement,
malformed/skipped records or inability to obtain consistent coverage; a partial
read cannot establish a complete session total.
`selection` is a typed set of native identities/history/accounting options, not
a predicate callback over raw data or an evaluation-task model.

Dispatch through existing adapters. Extend their current native row feeds and
shared extraction helpers; collect usage before transcript/branch/text projection.
A usage load and an activity load use the same interpretation rules, not parallel
parsers. The Pi input must include all native entries for all-branch accounting;
active-branch projection happens afterward. SQLite reads retain current read-only,
WAL-visible behavior. Do not introduce process-global mutable state or new runtime
SDK dependencies.

Keep `ActivityEvent.usage` as a compatibility, non-additive view. Add explicit
`usage_ids` links only additively; do not change existing v1 transcript bytes or
old field meanings. Usage records are independent of event `seq`, so filtering,
text splitting and typed-only events cannot multiply or erase consumption.

Add detailed `UsageCapabilities` to the existing capability surface: verified
source variants, metric families, entity granularities, counter kinds, branch
views, auxiliary support and incremental support. Existing `usage_data` remains
a coarse compatibility flag, not a promise of complete records. Runtime-wide
capability and session-specific coverage remain separate.

### 6.2 Versioned CLI data

Add `schemas/usage.v1.json` and its identical packaged copy for a new
`sesskit.usage/v1` payload. Keep the existing CLI envelope and transcript v1
schema unchanged; the envelope version and dataset version are different things.
Publish the schema in wheel and source artifacts when implementation ships.

Proposed read-only command:

```text
sesskit usage SESSION_REF
sesskit usage SESSION_REF --detail records --limit 100
sesskit usage SESSION_REF --detail observations --limit 100
sesskit usage SESSION_REF --view active-branch
sesskit usage SESSION_REF --turn-id TURN_ID
sesskit usage SESSION_REF --message-id MESSAGE_ID
```

Use the existing ref resolver, offline adapter registry and JSON-first CLI
contract. Do not add a human TUI, a different envelope, interactive prompts,
network dependencies or implicit file writes for this feature.

- Default output is a bounded summary. `records` and `observations` are paged,
  with an opaque generation-bound continuation token, actual counts, `has_more`,
  read coverage, returned scope and truncation diagnostics. Follow-up requests
  return that token through `--cursor`. Invalid/stale tokens fail explicitly and
  provide a fresh-read command; `--limit`/`--cursor` are invalid in summary mode.
- Summary coverage is independent of the returned page size. A page's sum is
  never advertised as the session total. Exact `--turn-id` and `--message-id`
  selectors are mutually exclusive and report matched and unlinked-record counts.
  Unknown attribution is not a zero-token message; report unavailable coverage.
  Unsupported filters/options fail; they are not accepted and ignored.
- Native numeric metric names/evidence are core observations, not hidden behind
  `--include-native`. That option adds bounded metadata: numeric usage breakdowns
  and explicitly mapped scalars such as service tier, inference geography, speed,
  provider-cost markers, context limit, temperature, top-p, seed and token/reasoning
  limits. Unknown free-text/configuration objects are excluded, with explicit
  omission counts. It never dumps transcript content, credentials, headers,
  arbitrary environment or full native rows; source paths are excluded by default.
  New monetary/metric extensions cannot silently expand that boundary.
- Successful read with partial/not-recorded/unsupported coverage has exit 0 and
  explicit state. Unreadable or unsupported source-format load is an error
  envelope (exit 1), bad arguments exit 2, missing ref exit 3, ambiguous ref exit
  5. A known runtime with no usage capability differs from an unreadable format.
- Help/`describe`, stable diagnostics and schema examples ship with the command;
  no command is advertised before its implementation stage is accepted.

### 6.3 Incremental accounting (U4)

Use the existing source-reader machinery, but expose usage **upserts/removals**,
not just appended text events. Late usage, a final output count, corrected SQLite
rows and resolved overlap can revise an earlier usage id and summary.

A usage change result includes generation, cursor, reset, record revisions,
removals and summary watermark. Replacement/truncation/invalid cursors reset the
collection; an all-branch Pi reader must not erase past spend on a branch switch.
An active-branch selection may change its members and reports those changes.

Initial/reset synchronization and large updates are paged at a frozen source
watermark. Explicit `sync_complete`/`has_more` distinguish a full replacement from
one returned page; cursor commitment cannot skip undelivered changes. A growing
source is processed after that watermark. Resume state stays bounded and does
not serialize the whole history into a cursor. No-change polls retain metadata-
only behavior. Snapshot and fully drained incremental materialization must agree.
Do not reuse activity-reader generation/branch rules unchanged when their
projection scope differs from accounting scope.

## 7. Implementation stages and acceptance

No parser fixes are part of this design-only change. Each future stage is a
complete, tested slice; do not expose an unfinished command/capability.

| Stage | Work and owning locations | Gate |
|---|---|---|
| U1: native records | Reuse shared identity/relation/evidence/value types; add only missing common call data; usage-specific records/reconciliation module; existing `activity.py`, `activity_opencode.py`, native row feeds and adapter contracts | Codex request vs cumulative revisions; Pi non-text, independent/compaction/branch-summary/tool usage; Claude repeated ids and placeholder/final fields; OpenCode message/step/aggregate variants; retain unsupported Cursor/Kimi states |
| U2: summaries | Pure normalization, scope selection, counter epochs, record linking and `UsageSummary`; no price engine | All synthetic examples above; field-level completeness/conflicts; branch/fork/child overlap; safe currency sums; no unknown-as-zero or hidden dropped observations |
| U3: public snapshot delivery | Public `__init__` exports, adapter dispatch, `cli.py` usage subcommand, dual-copy JSON Schema, runnable integration recipes, current-contract documentation | Schema validation, envelope/ref/error/paging acceptance, native-oracle comparison and current v1 byte parity; independent-context CLI usability check; published artifact/install verification when shipping code |
| U4: incremental delivery | Existing reader feeds plus usage revisions and generation-bound synchronization | Chunk-boundary/late-final updates; no-change/append/branch/reset/WAL checks; snapshot equality after draining; bounded cursor, latency, memory and output sizes |

Required regression cases include malformed/partial records; zeros versus missing;
unknown models/providers; mixed models/tier changes; initialized error usage;
identical counts from different real calls; mismatched response/turn ids; more
than one cumulative update per turn; native counter correction/reset; incomplete
starting history; missing child files/cycles; fork copies; compaction without a
following text message; tool-only/thinking-only/empty responses; repeated pages
and resumed readers; unknown operation kinds; recorded-cost-only records; and
non-finite/fractional/negative metrics preserved with diagnostics. Also run the
generality cases in §3 and assert that core reconciliation has no runtime-name
branches or duplicated session/turn/message identity model.

Verification must use an independent native-data oracle, not just agreement
between SessKit's snapshot and reader sharing the same bug. Compare exact native
sessions and metrics, plus fixtures derived from documented shapes. After parser
or wiring changes follow [CONTRACT.md](CONTRACT.md#verification-mandatory-when-changing-parsers-or-load-wiring):
all affected supported runtimes, success and reproducible abnormal paths,
`status_tag`/`last_agent_msg`/`load_events`, complete diagnostics and existing
platform/Python coverage. Unsupported or missing runtime samples are limits,
not passes. Measure cold load, warm/no-change polls and retained memory before
and after; do not benchmark only tiny histories. Acceptance targets: no-change
polls parse zero rows; steady append work is proportional to new records plus
bounded rechecks and affected links, not all raw history; retained memory holds
normalized records/indexes rather than duplicate full-file/line buffers; cursor
and returned page sizes are bounded independently of total history size.
Model/provider groups in default summaries are bounded too, with explicit omitted
counts and a paged detail route, never silent loss.

No new service, runtime execution dependency, pricing package, database or
persistent background process is required. Do not migrate derived caches or
release a new artifact for this design-only delivery. When code is implemented,
versioned artifact handoff and consumer acceptance follow the existing project
contract, without forcing evaluation behavior on any consumer.

## 8. References

- [C1: Claude Agent SDK cost/usage tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking): deduplication, output placeholders, restored/cumulative totals, subagent inclusion and estimate limits.
- [A1: Anthropic prompt-cache usage](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#tracking-cache-performance): uncached/cache-read/cache-creation input arithmetic and cache-retention breakdowns.
- [X1: Codex protocol source](https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/protocol/src/protocol.rs): native usage/counter vocabulary; local persisted shapes are separately sampled above.
- [X2: Codex non-interactive JSON](https://developers.openai.com/codex/noninteractive): `turn.completed.usage`; not proof of a persistence format.
- [O1: OpenCode native usage mapping](https://github.com/anomalyco/opencode/blob/d80f0fab1cd8169abef715209a55e35bb37071ee/packages/core/src/session/usage.ts), [assistant schema](https://github.com/anomalyco/opencode/blob/d80f0fab1cd8169abef715209a55e35bb37071ee/packages/schema/src/session-message.ts): exclusive native token buckets, cost-source selection and model/retry/timestamp data.
- [P1: Pi session format](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/coding-agent/docs/session-format.md): usage-only records, compaction/branch-summary accounting and context edits.
- [P2: Pi message types](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/coding-agent/docs/message-types.md): response/model/reasoning metadata, nested tool usage and subset arithmetic.
- [P3: Pi usage types](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/ai/src/types.ts), [session statistics](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/coding-agent/src/core/agent-session.ts), [catalog cost calculation](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/ai/src/models.ts): all-entry accounting, exclusive input/cache buckets and per-amount provider-versus-estimate provenance.
- [R1: Cursor CLI output](https://cursor.com/docs/cli/reference/output-format): message/call identifiers and the documented timing limitation; no verified local token mapping inferred from it.
- [L1: Langfuse token/cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking): ingested versus inferred values and generation-level collection.
- [T1: OpenTelemetry GenAI attributes](https://github.com/open-telemetry/semantic-conventions-genai/blob/3fe84604a581b3d50dd5b4bd29383b462d2a5018/docs/registry/attributes/gen-ai.md): inclusive totals, token subsets and distinct billed-unit semantics; Development stability.

Search-service quota was exhausted during this investigation. Findings above
were checked through official pages, Context7 and inspected upstream/local
source; this is not a claim of an exhaustive market survey.

[C1]: https://code.claude.com/docs/en/agent-sdk/cost-tracking
[A1]: https://platform.claude.com/docs/en/build-with-claude/prompt-caching#tracking-cache-performance
[X1]: https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/protocol/src/protocol.rs
[X2]: https://developers.openai.com/codex/noninteractive
[O1]: https://github.com/anomalyco/opencode/blob/d80f0fab1cd8169abef715209a55e35bb37071ee/packages/core/src/session/usage.ts
[P1]: https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/coding-agent/docs/session-format.md
[P2]: https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/coding-agent/docs/message-types.md
[P3]: https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/ai/src/types.ts
[R1]: https://cursor.com/docs/cli/reference/output-format
[L1]: https://langfuse.com/docs/observability/features/token-and-cost-tracking
[T1]: https://github.com/open-telemetry/semantic-conventions-genai/blob/3fe84604a581b3d50dd5b4bd29383b462d2a5018/docs/registry/attributes/gen-ai.md
