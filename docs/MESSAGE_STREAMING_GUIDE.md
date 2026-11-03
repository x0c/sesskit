# Message streaming

Use SessKit to display an existing agent session incrementally without maintaining
six native parsers. SessKit observes; the application owns presentation, native
transport, access control and execution. No agent is launched, resumed or interrupted.

This API is in the current source checkout, not the published v0.2.24 release wheel.
Install the checkout with `python -m pip install .` using the application's Python
3.10+ environment. macOS and Linux are supported; no extra dependency is required.

## Python

A session is the same dictionary returned by a runtime scanner:

```python
from sesskit import get_adapter, open_message_stream

sessions = get_adapter("pi").scan(limit=5)
if sessions:
    session = sessions[0]
    with open_message_stream(session) as stream:
        for record in stream.follow(polls=3, interval=0.25):
            if record.type == "upsert":
                print(record.key, record.event["type"], record.event.get("text"))
```

`poll()` performs one nonblocking native-feed drain plus a history read and returns
a tuple of records; it does not sleep. `follow()` adds polling waits. Give `polls`
for bounded use, or a `threading.Event` as `stop_event` for application shutdown.
The Python caller owns its overall time/output budget. Use the context manager or
`close()` when no longer observing. Records are immutable outer dataclasses;
`message_stream_record_to_dict(record)` returns a plain JSON-ready copy.

## Feed an existing native subscription

```python
from sesskit import NativeEventSource, open_message_stream

source = NativeEventSource()
# The existing execution/transport owner forwards each decoded native JSON object:
source.feed({
    "type": "message_update",  # Pi SDK/RPC event in this example
    "message": {"role": "assistant", "content": [{"type": "text", "text": "Still working"}]},
})
with open_message_stream(session, live_source=source) as stream:
    for record in stream.poll():
        print(record.type, record.mode, record.event)
```

The host can register `source.feed` with its SDK callback or call it from its
WebSocket/SSE reader. It returns a boolean and copies accepted JSON so later
producer mutation cannot change queued records. It does not propagate malformed
input or overflow exceptions into an agent callback. Call `source.disconnect()`
on a transport failure and `source.reconnect()` after the host reconnects; the
stream changes mode automatically. Transport exceptions, credentials and account
selection stay with the existing owner, not in SessKit configuration.

The input understands these inspected native event shapes:

| Runtime | Host-supplied input |
|---|---|
| Claude | CLI/Agent SDK `stream_event` with partial messages, plus final assistant/result objects |
| Codex | Shared app-server JSON notifications, not `codex exec --json` text deltas |
| OpenCode | v2 `/api/event` native events; v1 `message.part.delta` / `message.part.updated` envelopes |
| Kimi | Web `transcript.reset` / `transcript.ops`, including UTF-16 append offsets |
| Cursor | CLI `stream-json --stream-partial-output`; duplicate buffered/final flushes are skipped |
| Pi | SDK/extension/RPC agent events, including cumulative message updates and message end |

This is **format normalization, not automatic cross-process attachment**. A session
ID alone cannot expose a private process's live feed. The six-runtime source pins,
protocol caveats and unperformed live attachment experiments remain in the
[research knowledge base](STREAMING_OUTPUT_KNOWLEDGE_BASE.md). Legacy Kimi wire or
ACP formats not listed here are not advertised as native-fragment inputs.

Only events bound to the selected session are accepted when native session identity
is present. Where the protocol omits it, the host must supply an already scoped
feed. Do not mix different users/accounts into an unscoped input. Native images,
private reasoning not emitted by the source, terminal repaint bytes, launch/control
commands and incremental usage accounting are not synthesized.

## Automatic degradation and recovery

- Start with persisted history, including while a configured feed is still waiting
  for usable events. Foreign-session events and heartbeats do not erase that view.
- Switch to live mode when usable native updates arrive. If the feed fails, ends,
  overflows or becomes malformed, continue with history automatically. If no store
  is readable, emit `unavailable` and preserve the last view until recovery.
- A source switch or history replacement emits `reset`. Replace the observation
  window; do not merge incompatible live/history identities by guessing from text.
  The history view is authoritative for what persisted. An unpersisted fragment
  may disappear on reconciliation; `gap` explicitly reports possible lost fragments.
- A healthy reconnected feed switches back to live on its next usable update.
  There is no promise to replay volatile fragments or reconstruct original timing.

History uses the five existing incremental readers. Kimi uses the existing public
conversation loader and stat-versioned snapshots, with inferred positional identity
and unknown turn outcome; its native parser is not duplicated or silently promoted
to typed/incremental activity support.

## Applying records

The schema is `sesskit.message-stream/v1`, shipped in both schema directories.

1. `reset`: discard the displayed observation window for that generation.
2. `upsert`: replace the whole value at `key` with `event`; **never append its text**.
   `event` reuses the existing typed activity fields, including native message/turn
   IDs when available. A key is a local projection identity, not an invented native
   message ID. Final native snapshots replace accumulated partial text.
3. `remove`: remove that key (including bounded-window eviction).
4. `state`: inspect `mode`, source `state`, `history_state`, `reason` and `gap`.
5. `checkpoint`: save its opaque `cursor` only after applying preceding records.
6. `end`: this observation ended, **not the agent**. Check `error` and the process
   exit code. `outcome` on checkpoints is the selected source's last evidenced
   historical turn, never process liveness; EOF alone is never success evidence.

The retained display window is 1–200 events (default 200), not a full transcript.
`truncated` reports tail/eviction/budget limits. Retained existing reader indexes
have their own memory contracts. Multiple updates to one key in a poll coalesce to
its newest value, so this is a display stream, not a lossless token-timing trace.
Cursors bind session and window size. Restart/reconnect reconciles an explicit
reset rather than promising exactly-once delivery across incompatible sources.

## Any language: JSONL CLI

```bash
sesskit stream pi:SESSION_ID --once
sesskit stream pi:SESSION_ID --polls 20 --interval 0.1
sesskit stream pi:SESSION_ID --unbounded
sesskit stream pi:SESSION_ID --input native-events.jsonl --polls 3
sesskit stream opencode:SESSION_ID --input - --input-format sse --unbounded
sesskit describe stream
```

Each stdout line is a **bare stream record**, including pre-start failures as an
`end` record with structured `error`. Other commands retain their single-envelope
JSON contract. No progress text or raw transport exception is mixed into stdout.
The last example consumes the **host's existing** SSE connection; SessKit does not
start a server or acquire credentials. Input files/pipes are borrowed, not controlled.
Do not mix buffered reads with the descriptor input. A finite input reaching EOF
means automatic history degradation, not a completed agent turn.

Default CLI budget: **60 seconds / 1000 total records / 256 KiB**, whichever comes
first. `--duration`, `--max-records` and `--max-bytes` set independent limits;
`--unbounded` explicitly lifts all three and rejects simultaneous budget options.
`--once`/`--polls` bound polling independently. Budget expiry emits a normal `end`
with `time_limit`, `record_limit` or `byte_limit`, sets `truncated`, and never splits
a message/JSON record. Time is checked between polls; an in-progress native read
finishes first. Use the last emitted checkpoint when resuming after cutoff.

Exit 0 means observation ended normally, including usable degraded history or a
budget cutoff; 1 means no usable source/observer failure; 2 invalid arguments;
3 not found; 5 ambiguous; 130 keyboard interruption. Native agent failure remains
an event/outcome and does not imply the observation itself failed.

Closing a callback bridge sends no cancellation. The host must unregister its
SDK callback when done. Closing a CLI input pipe can be visible to its producer
(e.g. broken pipe); keep the execution owner's transport alive independently if
that native producer couples stdout closure to process exit. SessKit never claims
that an arbitrary owned child-process pipe has harmless detach semantics.

Native message/tool/error content may contain private code or credentials. Protect
output and authorization in the consuming application. Nothing is uploaded or
written to native history by this stream.

## Verification and limits

- macOS Python 3.10–3.15: 1021 tests passed per interpreter; one historical Claude
  incident fixture is absent. Linux Python 3.10: 997 passed, 25 platform/local-history
  checks skipped because that node has no agent stores or macOS process APIs.
  Build and Ruff diagnostics contain no retained warnings.
- The 66 focused stream/recovery regressions include all five reader families
  recovering an identical restored source and unavailable-cursor reopens. The
  Linux-found stale-unavailable cache defect was fixed in the shared readers,
  not hidden by sleeps or weakened fixture checks.
- An independently running consumer process read the installed wheel's CLI,
  received a native-format partial before its synthetic producer finalized,
  then observed EOF-driven history degradation and one reconciled final reply.
  This is a real package/pipe/consumer path, not a fresh model/provider experiment.
- Actual native-history reads covered five sessions each for Pi, Claude, Codex,
  OpenCode and Cursor. Kimi has six archived stores, only three with printable
  conversation data (four messages total); the larger nonempty-sample target is
  explicitly unverified, not padded with empty stores or invented model runs.
  Kimi legacy outcome remains unknown by contract.
- Host-created SDK/WebSocket connections, permissions, reconnect handshakes and
  native cancellation semantics are not accepted merely by testing event formats.
  No automatic cross-process attachment or raw terminal-byte support is claimed.

Complete task evidence is retained privately under
`~/.config/sesskit/development/message-stream-20261103-6dda6090/`; it includes the
initial Linux failure, complete platform diagnostics and consumer/native receipts.
Native conversations and credentials are not included in this repository.
