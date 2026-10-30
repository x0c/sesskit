# Integration guide

Build a local history search tool, an analysis script, or a session client without
maintaining a parser for each coding agent. SessKit reads native history; your
application owns indexing, presentation, transport, and actions.

## Scope and integration requirements

External developers must be able to adopt SessKit without any downstream host.
First-use examples cover search ingestion, native usage and outcome inspection,
and incremental consumption. They use public APIs only, do not launch agents or
modify history, and introduce no telemetry, network service, or runtime dependency.
The CLI envelope and existing schemas remain unchanged. Examples are application
recipes, not additional stable CLI commands or a new typed JSON schema.

## Install a fixed release

SessKit is not on PyPI. On macOS and Linux, with Python 3.10+ and pip, activate your
project's virtual environment and install the published wheel. If you need a new
environment, run `python3 -m venv .venv && . .venv/bin/activate` first.

```bash
python -m pip install "https://github.com/x0c/sesskit/releases/download/v0.2.21/sesskit-0.2.21-py3-none-any.whl"
python -m sesskit describe
```

This avoids Git and local build tools. Use the same interpreter for installation
and your application. For a standalone CLI, `pipx install` accepts the same wheel
URL. Windows is not a supported platform.

The examples below live in the repository's `examples/` directory and source
archives built from this checkout. The published 0.2.21 wheel and source archive
predate these recipes: download the three Python files from GitHub or clone the
repository, and run them with the interpreter above. The wheel installs only the
library and CLI. They also work with the fixed release; a source installation
or `PYTHONPATH` is not needed. Review output locally: export and follow output may
contain private prompts, code, tool output, or credentials from native history.
Nothing is uploaded automatically. Do not redirect output onto native history.

## 1. Feed your own search index

Source: [export_for_search.py](../examples/export_for_search.py).

```bash
python examples/export_for_search.py --runtime claude --limit 5
```

One JSON line per session, with `id`, `runtime`, `session_id`, `activity_time`,
`message_count`, and `text`. The id includes the runtime, so equal native ids
from different agents do not collide. Replace the whole document on re-import;
history can branch or change, so this is not an append-only message index.
Plain conversation includes error-only assistant messages in this recipe, but
excludes injected/system context according to SessKit's conversation contract.
This example does not implement a search engine, embeddings, or an upload.

## 2. Inspect native usage and outcomes

Source: [usage_and_outcomes.py](../examples/usage_and_outcomes.py).

```bash
python examples/usage_and_outcomes.py --runtime pi --limit 5
```

One JSON report contains scanned/loaded/unsupported/failed counts, historical
outcome counts, and per-session usage observations. It excludes conversation text
and raw error messages. Unsupported typed activity, unavailable history, empty
history, and supported-but-unrecorded usage are different states.

Usage observations preserve `seq`, `message_id`, `turn_id`, native values, and
evidence. **They are not additive billing rows.** One native message's usage can
appear on both thinking and text events; turn usage can appear on multiple
messages. The recipe intentionally does not sum tokens or costs. Missing values
stay null, not zero; no price lookup or cost estimate is performed. Session
outcomes describe persisted history, not whether a process is currently running.

Reconciled records and scoped summaries are available in code and on the CLI:

```bash
sesskit usage pi:SESSION_ID
sesskit usage pi:SESSION_ID --detail records --limit 100
sesskit usage pi:SESSION_ID --message-id MESSAGE_ID
```

```python
from sesskit import load_usage, summarize_usage, UsageSelection

snapshot = load_usage(session)  # session dict from scan; state is
                                # available/empty/unavailable/unsupported
summary = summarize_usage(snapshot)
narrow = summarize_usage(snapshot, selection=UsageSelection(message_ids=("m1",)))
```

`summary.known_total` sums only proven non-overlapping records; a stored
session rollup only ever sets `complete_total`, never joins the sums. Every
total carries its coverage (`complete`/`partial`/`unknown`/`conflicted`/
`not_recorded`/`unsupported`) and contributing record ids. The versioned
`sesskit.usage/v1` payload is described by `schemas/usage.v1.json` (duplicated
under `src/sesskit/schemas/`). The [native usage data design](NATIVE_USAGE_DATA_DESIGN.md)
specifies the full reconciliation rules. SessKit supplies native data;
evaluation and the meaning of an external task remain with the consumer.

## 3. Follow one session

Source: [follow_session.py](../examples/follow_session.py).

Copy the full native session id from `sesskit list --top 5 --compact`, then run:

```bash
python examples/follow_session.py --runtime pi --session-id SESSION_ID --polls 3
```

The example resolves an exact id within the selected runtime and emits one JSON
line per poll, including events, state, outcome, generation, reset, and the opaque
cursor. Polling is bounded (`--polls`, default 3); no background process remains.
Use `--interval` to change the delay in seconds.

- On `reset: true`, discard the previous generation before applying these events.
  The returned events may be only a tail window, not the complete history. Use
  `reader.page(before=..., limit=...)` to obtain older pages.
- Keep event sequence numbers within their generation. Join tool calls and results
  by `call_id`, not adjacency. Keep the cursor opaque; an application may persist
  it and pass it back to `open_activity_reader(session, cursor=saved_cursor)`.
- No-change checks are metadata-only. Same-size writes that preserve the observed
  file timestamps can remain invisible until a later stamp change or reload;
  this is not a lossless filesystem event subscription.
- A no-change poll with no events is not a completed turn. Inspect the historical
  outcome and its evidence separately from your application's process observation.
- Late tool/usage/interaction linkage can enrich the reader's materialized events
  without re-emitting earlier events. Reconcile from pages or a fresh snapshot
  when your application needs enriched prior records. Polls are not a universal
  patch stream.
- Unsupported incremental reading is an explicit error. A host can instead use
  `load_activity` snapshots when `capabilities.typed_activity` is true.

The follow recipe serializes Python dataclasses for illustration. Its JSON is not
`transcript.v1` and must not be treated as a separately versioned public schema.

## Discovery, failures, and coverage

Every example requires an explicit `--runtime`. `--limit` bounds discovery (5 for
export/report, 200 for exact-id follow); it does not promise an exhaustive archive.
If an id is not found, increase the limit or use `--include-missing-cwd` for
sessions whose project no longer exists. Automation workspaces remain excluded.
Search ingestion is full conversation text for discovered sessions; the existing
`sesskit search` command itself searches titles and excerpts, not full history.

These recipes use pipeline-oriented output: JSON/JSONL data on stdout, sanitized
JSON diagnostics on stderr, exit 0 on success, 1 on read/unsupported failure,
2 on invalid arguments, and 3 on an exact id not found. A partial export may have
already emitted valid documents before a later read fails; check the exit code.
The usage report retains per-session failure records and exits nonzero on any
failure. Empty discovery is reported explicitly on stderr for streams and in the
report's counts; it is not proof that no history exists outside the scan scope.
Handled read failures are identified by type, without dumping private paths or
native content. For the stable CLI envelope use `python -m sesskit` instead.

## Capability matrix

Capabilities describe verified adapter support, not a guarantee that every
session contains the data. No means unsupported or unverified, not evidence that
the runtime itself cannot provide it. Query your installed version rather than
branching on runtime names:

```python
from sesskit import get_adapter

capabilities = get_adapter("pi").capabilities
if capabilities.incremental_reading:
    # Open an incremental reader for a session discovered by the registry.
    pass
```

Current adapter declarations (also returned by `sesskit describe`):

| Runtime id | Typed activity | Incremental | Structured questions | Native turn markers | Usage | Compaction |
|---|---|---|---|---|---|---|
| claude | Yes | Yes | Yes | Yes | Yes | Yes |
| codex | Yes | Yes | Yes | Yes | Yes | Yes |
| opencode | Yes | Yes | Yes | Yes | Yes | Yes |
| kimi | No | No | No | No | No | No |
| cursor | Yes | Yes | Yes | No | No | No |
| pi | Yes | Yes | No | Yes | Yes | Yes |

All six adapters retain plain conversation and v1 transcript access. Kimi's typed
adaptation is deferred. None currently declares verified permission-record
support. Native tool-result status, message origin, and relation capabilities are
also available through `get_adapter(...).capabilities` / `sesskit describe`.

## Choosing an interface

- **Python:** use registry discovery, `load_session_conversation`, `load_activity`,
  and `open_activity_reader`. Do not call path-based parser internals.
- **Other languages:** invoke `sesskit` as a subprocess, check both exit status and
  `ok`, and consume the published [schemas](../schemas/). `share` uses transcript
  v1, a compatibility projection that omits some richer Python fields, including
  usage and evidence. No equally rich, versioned typed JSON schema is promised.
- **Verification:** `sesskit verify` checks internal consistency, not independent
  semantic correctness. Missing samples are unverified, not passes. Full
  contracts and reader limits are in [CONTRACT.md](CONTRACT.md).

## Tell us what you are building

[Open an integration feedback issue](https://github.com/x0c/sesskit/issues/new?template=integration.yml)
with the tool you are building, runtimes, desired output, and what blocked you.
No account credentials or complete history are needed. A minimal synthetic or
redacted sample is preferable; review it before posting in a public issue.
Stars indicate interest, not a validated use case. Prioritize concrete integration
obstacles and parser correctness over runtime counts or unrelated host features.

## Validation scope

The first-use recipes were checked on 2026-10-26 without changing parser code,
public APIs, schemas, or the published version:

- macOS, Python 3.10 and 3.15: 650 tests and 25 subtests passed on each interpreter.
- Linux, Python 3.10: 625 tests and 17 subtests passed; 22 real-history tests had no
  discoverable native samples and 3 process-snapshot tests were macOS-only. All
  25 are explicitly skipped, not verified. The extracted source archive also
  passed its full applicable suite.
- The 23 recipe regressions cover real scan/load wiring on owned fixtures, empty
  discovery, partial/missing-history failures, sanitized diagnostics, zero versus
  missing usage, unsupported capabilities, exact ids, append, and branch reset.
- A clean environment installed the published 0.2.21 wheel with no source-path
  override. All three recipes returned real data for one local history from each
  of Claude, Codex, OpenCode, Cursor, and Pi (15 recipe runs). Follow used two polls
  per session. This is read-only integration evidence, not fresh model execution
  or an independent semantic oracle.
- Ruff, wheel/sdist builds, relative links/anchors, root navigation, and the issue
  form's JSON Schema validation passed without retained diagnostics. The source
  archive includes the examples and their documentation; the wheel remains a
  library/CLI artifact. No public issue was created during verification.
- No independent external developer usability trial has been run. The feedback
  form is an invitation, not evidence of adoption or demand.

## Reference decisions

- [session](https://github.com/nitsanavni/session): inspected its parse/export and
  stream examples. Reuse task-oriented presentation, not its browser/resume scope.
- [LLM](https://github.com/simonw/llm): inspected its CLI/library quick start. Reuse
  a short install-to-first-result route, not model execution or credential setup.
- [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) and
  [Langfuse](https://langfuse.com/resources/engineering/coding-agent-tracing)
  demonstrate search and analysis consumers; they do not establish our users'
  intentions and their application features do not belong in SessKit.
- [pip install](https://pip.pypa.io/en/stable/cli/pip_install/) documents installing
  archive URLs; the pinned wheel path above is verified against release assets.
- [GitHub issue forms](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)
  supplies the feedback form's YAML structure; no private contact data is required.
