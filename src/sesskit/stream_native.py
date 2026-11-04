"""Native live-event interpretation, separate from persisted-history parsers.

Reference formats and pins are documented in STREAMING_OUTPUT_KNOWLEDGE_BASE.
Only emitted content is projected. Final snapshots replace, never append to,
partial values. Missing replay/offset information is not invented.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from sesskit.models import ActivityEvent, AgentError, Evidence, SessionOutcome


@dataclass(frozen=True)
class NativeUpdate:
    key: str
    event: ActivityEvent | None
    complete: bool | None = None
    operation: str = "upsert"


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class NativeEventNormalizer:
    """One bound session's live projection. Keys are local projection identities."""

    def __init__(self, runtime: str, session_id: str, *, limit: int = 200):
        if runtime not in {"claude", "codex", "opencode", "kimi", "cursor", "pi"}:
            raise ValueError("native streaming is unsupported for this runtime")
        self.runtime, self.session_id, self.limit = runtime, session_id, limit
        self._values: OrderedDict[str, NativeUpdate] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seq = 0
        self._message = ""
        self._counter = 0
        self._cursor_partial = False
        self._kimi_seqs: dict[str, int] = {}
        self.gap = False
        self._pi_can_complete = False
        self._opencode_roles: OrderedDict[str, str] = OrderedDict()
        self._opencode_parts: OrderedDict[str, str] = OrderedDict()
        self._claude_blocks: OrderedDict[str, int] = OrderedDict()
        self.outcome = SessionOutcome("unknown", Evidence("unknown"))

    def _next_message(self, native: object = None) -> str:
        self._counter += 1
        self._message = _text(native) or f"local:{self._counter}"
        return self._message

    def _emit(self, key: str, kind: str, *, text: str | None = None,
              append: bool = False, complete: bool | None = None,
              message_id: str | None = None, turn_id: str | None = None,
              name: str | None = None, call_id: str | None = None,
              raw_input: object = None, raw_output: object = None,
              error: AgentError | None = None, stop_reason: str | None = None) -> list[NativeUpdate]:
        old = self._values.get(key)
        if append:
            if old is None:
                self.gap = True
            text = ((old.event.text or "") if old and old.event else "") + (text or "")
        if text is not None and len(text) > 1 << 20:
            raise ValueError("live message exceeds text limit")
        if old and old.event:
            seq = old.event.seq
        else:
            self._seq += 1
            seq = self._seq
        event = ActivityEvent(
            seq=seq, type=kind, evidence=Evidence("native", field="live_event"),
            message_id=message_id, turn_id=turn_id, text=text, name=name,
            call_id=call_id, raw_input=raw_input, raw_output=raw_output,
            error=error, stop_reason=stop_reason,
        )
        update = NativeUpdate(key, event, complete)
        if update == old:
            return []
        self._values[key] = update
        while len(self._values) > self.limit:
            self._values.popitem(last=False)
        return [update]

    def _finish(self, status: str, error: object = None, *, turn_id: str | None = None,
                key: str | None = None) -> list[NativeUpdate]:
        if status not in {"done", "pending", "aborted", "unknown"}:
            raise ValueError("invalid native outcome")
        message = (_text(error) or _text(_obj(error).get("message"))
                   or _text(_obj(_obj(error).get("data")).get("message")))
        failure = AgentError("provider", message, Evidence("native", field="live_event"), scope="turn") if message else None
        if failure:
            status = "aborted"
        self.outcome = SessionOutcome(status, Evidence("native", field="live_event"), failure)
        return self._emit(f"terminal:{key or turn_id or self._counter}", "lifecycle", error=failure,
                          stop_reason=status, turn_id=turn_id, complete=True)

    def normalize(self, raw: dict) -> list[NativeUpdate]:
        # All-server feeds must not leak a different session into this projection.
        event = _obj(raw.get("payload")) if self.runtime == "opencode" and "directory" in raw else raw
        params = _obj(event.get("params"))
        data = _obj(event.get("data")) or _obj(event.get("properties"))
        scope = (event.get("session_id") or event.get("sessionID") or params.get("threadId")
                 or data.get("sessionID"))
        if scope is not None and scope != self.session_id:
            return []
        event_id = event.get("id") if self.runtime == "opencode" else event.get("uuid")
        if isinstance(event_id, str):
            if event_id in self._seen:
                return []
            self._seen[event_id] = None
            if len(self._seen) > 4096:
                self._seen.popitem(last=False)
        return getattr(self, f"_{self.runtime}")(event)

    def _blocks(self, message: dict, *, complete: bool, pi: bool = False,
                start: int = 0) -> list[NativeUpdate]:
        mid = _text(message.get("id"))
        identity = mid or self._message or self._next_message()
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or len(content) > 1000:
            raise ValueError("invalid native content blocks")
        role = message.get("role", "assistant")
        if role not in {"user", "assistant", "toolResult"}:
            return []
        if role == "toolResult":
            return self._emit(f"{identity}:result", "tool_result", message_id=mid or None,
                              call_id=_text(message.get("toolCallId")) or None,
                              raw_output=content, complete=complete)
        updates = []
        for offset, block in enumerate(content):
            index = start + offset
            block = _obj(block)
            typ = block.get("type")
            kind = "user_message" if role == "user" else "assistant_message"
            text = _text(block.get("text"))
            if typ in {"thinking", "reasoning"}:
                kind, text = "thinking", _text(block.get("thinking")) or _text(block.get("text"))
            elif typ in {"tool_use", "toolCall"}:
                updates += self._emit(f"{identity}:{index}", "tool_call", message_id=mid or None,
                                      call_id=_text(block.get("id")) or None, name=_text(block.get("name")) or None,
                                      raw_input=block.get("arguments" if pi else "input"), complete=complete)
                continue
            elif typ == "tool_result":
                updates += self._emit(f"{identity}:{index}", "tool_result", message_id=mid or None,
                                      call_id=_text(block.get("tool_use_id")) or None,
                                      raw_output=block.get("content"), complete=complete)
                continue
            elif typ != "text":
                continue
            updates += self._emit(f"{identity}:{index}", kind, text=text, message_id=mid or None, complete=complete)
        return updates

    def _claude(self, raw: dict) -> list[NativeUpdate]:
        typ = raw.get("type")
        if typ in {"assistant", "user"}:
            message = dict(_obj(raw.get("message")))
            message.setdefault("role", typ)
            if typ == "user":
                message.setdefault("id", raw.get("uuid"))
                self._next_message(message.get("id"))
                return self._blocks(message, complete=True)
            content = message.get("content", [])
            is_single = isinstance(content, str) or (isinstance(content, list) and len(content) == 1)
            if is_single:
                candidate = _text(message.get("id")) or self._message
                if candidate and candidate in self._claude_blocks:
                    return self._blocks(message, complete=True,
                                        start=self._claude_blocks[candidate])
            return self._blocks(message, complete=True)
        if typ == "result":
            error = "; ".join(e for e in raw.get("errors", []) if isinstance(e, str))
            return self._finish("aborted" if raw.get("is_error") else "done",
                                error or (raw.get("result") if raw.get("is_error") else None),
                                key=_text(raw.get("uuid")) or None)
        if typ != "stream_event":
            return []
        event = _obj(raw.get("event"))
        typ = event.get("type")
        if typ == "message_start":
            mid = _text(_obj(event.get("message")).get("id"))
            self._next_message(_obj(event.get("message")).get("id"))
            if mid and mid in self._claude_blocks:
                del self._claude_blocks[mid]
            return []
        identity = self._message or self._next_message()
        index = event.get("index", 0)
        key = f"{identity}:{index}"
        mid = None if identity.startswith("local:") else identity
        if typ == "content_block_start":
            if type(index) is int and 0 <= index < 1000:
                self._claude_blocks[identity] = index
                self._claude_blocks.move_to_end(identity)
                while len(self._claude_blocks) > self.limit:
                    self._claude_blocks.popitem(last=False)
            block = _obj(event.get("content_block"))
            if block.get("type") == "tool_use":
                return self._emit(key, "tool_call", message_id=mid, call_id=_text(block.get("id")) or None,
                                  name=_text(block.get("name")) or None, raw_input=block.get("input"), complete=False)
            kind = "thinking" if block.get("type") == "thinking" else "assistant_message"
            return self._emit(key, kind, text=_text(block.get("thinking" if kind == "thinking" else "text")),
                              message_id=mid, complete=False)
        if typ == "content_block_delta":
            delta = _obj(event.get("delta"))
            if delta.get("type") in {"text_delta", "thinking_delta"}:
                thinking = delta["type"] == "thinking_delta"
                return self._emit(key, "thinking" if thinking else "assistant_message",
                                  text=_text(delta.get("thinking" if thinking else "text")),
                                  append=True, message_id=mid, complete=False)
        if typ == "content_block_stop" and key in self._values:
            value = self._values[key]
            result = replace(value, complete=True)
            self._values[key] = result
            return [result] if result != value else []
        return []

    def _pi(self, raw: dict) -> list[NativeUpdate]:
        typ, message = raw.get("type"), _obj(raw.get("message"))
        if typ == "message_start":
            self._next_message(message.get("id"))
        if typ in {"message_start", "message_update", "message_end"}:
            if typ == "message_update" and not message.get("content"):
                delta = _obj(raw.get("assistantMessageEvent"))
                if delta.get("type") in {"text_delta", "thinking_delta"}:
                    kind = "thinking" if delta["type"] == "thinking_delta" else "assistant_message"
                    return self._emit(f"{self._message}:{delta.get('contentIndex', 0)}", kind,
                                      text=_text(delta.get("delta")), append=True, complete=False)
            result = self._blocks(message, complete=typ == "message_end", pi=True)
            if typ == "message_end" and message.get("role") == "assistant":
                self._pi_can_complete = message.get("stopReason") in {"stop", "length"}
                if message.get("stopReason") in {"error", "aborted"}:
                    result += self._finish("aborted", message.get("errorMessage"))
            return result
        if typ == "agent_end":
            return [] if self.outcome.status == "aborted" else self._finish("done" if self._pi_can_complete else "unknown")
        if typ == "turn_start":
            self._pi_can_complete = False
            self.outcome = SessionOutcome("pending", Evidence("native", field="live_event"))
        return []

    def _cursor(self, raw: dict) -> list[NativeUpdate]:
        typ = raw.get("type")
        if typ == "assistant":
            partial = "timestamp_ms" in raw and "model_call_id" not in raw
            if self._cursor_partial and ("model_call_id" in raw or not partial):
                return []
            self._cursor_partial |= partial
            message = _obj(raw.get("message"))
            identity = self._message or self._next_message(message.get("id"))
            content = message.get("content", [])
            if not isinstance(content, list):
                raise ValueError("invalid Cursor content")
            text = "".join(_text(_obj(b).get("text")) for b in content if _obj(b).get("type") == "text")
            return self._emit(f"{identity}:text", "assistant_message", text=text, append=partial,
                              complete=not partial, message_id=_text(message.get("id")) or None)
        if typ == "tool_call":
            self._next_message()
            return []
        if typ == "result":
            updates = []
            for key, value in list(self._values.items()):
                if value.event and value.event.type == "assistant_message" and value.complete is False:
                    final = replace(value, complete=True)
                    self._values[key] = final
                    updates.append(final)
            return updates + self._finish("aborted" if raw.get("is_error") else "done",
                                          raw.get("result") if raw.get("is_error") else None)
        return []

    def _codex(self, raw: dict) -> list[NativeUpdate]:
        method, data = raw.get("method"), _obj(raw.get("params"))
        mid, tid = _text(data.get("itemId")), _text(data.get("turnId")) or None
        deltas = {"item/agentMessage/delta": "assistant_message", "item/reasoning/textDelta": "thinking",
                  "item/reasoning/summaryTextDelta": "thinking", "item/commandExecution/outputDelta": "tool_result"}
        if method in deltas:
            if not mid:
                raise ValueError("Codex delta has no item identity")
            suffix = f":{data.get('summaryIndex', data.get('contentIndex', 0))}" if deltas[method] == "thinking" else ""
            return self._emit(mid + suffix, deltas[method], text=_text(data.get("delta")), append=True,
                              message_id=mid, turn_id=tid, complete=False)
        if method in {"item/started", "item/completed"}:
            item = _obj(data.get("item"))
            mid, typ = _text(item.get("id")), item.get("type")
            if typ == "agentMessage" and mid:
                return self._emit(mid, "assistant_message", text=_text(item.get("text")),
                                  message_id=mid, turn_id=tid, complete=method == "item/completed")
        if method == "turn/completed":
            turn = _obj(data.get("turn"))
            status = {"completed": "done", "failed": "aborted", "interrupted": "aborted"}.get(turn.get("status"), "unknown")
            return self._finish(status, turn.get("error"), turn_id=_text(turn.get("id")) or tid)
        if method == "error":
            return self._finish("aborted", data.get("error"), turn_id=tid)
        return []

    def _opencode(self, raw: dict) -> list[NativeUpdate]:
        typ = _text(raw.get("type"))
        data = _obj(raw.get("data")) or _obj(raw.get("properties"))
        if typ.startswith(("session.text.", "session.reasoning.")):
            mid = _text(data.get("assistantMessageID"))
            if not mid:
                raise ValueError("OpenCode delta has no message identity")
            kind = "thinking" if typ.startswith("session.reasoning.") else "assistant_message"
            return self._emit(f"{mid}:{kind}:{data.get('ordinal', 0)}", kind, message_id=mid,
                              text=_text(data.get("delta" if typ.endswith(".delta") else "text")),
                              append=typ.endswith(".delta"), complete=typ.endswith(".ended"))
        if typ == "message.updated":
            info = _obj(data.get("info"))
            if info.get("sessionID") not in (None, self.session_id):
                return []
            mid, role = _text(info.get("id")), info.get("role")
            if mid and role in {"assistant", "user"}:
                self._opencode_roles[mid] = role
                if len(self._opencode_roles) > self.limit:
                    self._opencode_roles.popitem(last=False)
            if role == "assistant" and info.get("error"):
                return self._finish("aborted", info["error"], key=mid or None)
            return []
        if typ == "message.part.delta":
            mid, part = _text(data.get("messageID")), _text(data.get("partID"))
            kind = self._opencode_parts.get(part)
            if data.get("field") != "text":
                return []
            if self._opencode_roles.get(mid) != "assistant" or kind not in {"text", "reasoning"}:
                self.gap = True
                return []
            return self._emit(part, "thinking" if kind == "reasoning" else "assistant_message",
                              text=_text(data.get("delta")), append=True, message_id=mid, complete=False)
        if typ == "message.part.updated":
            part = _obj(data.get("part"))
            if part.get("sessionID") not in (None, self.session_id):
                return []
            role = self._opencode_roles.get(_text(part.get("messageID")))
            if part.get("type") in {"text", "reasoning"} and role in {"user", "assistant"}:
                pid = _text(part.get("id"))
                if not pid:
                    raise ValueError("OpenCode part has no identity")
                self._opencode_parts[pid] = part["type"]
                if len(self._opencode_parts) > self.limit:
                    self._opencode_parts.popitem(last=False)
                kind = "thinking" if part["type"] == "reasoning" else "user_message" if role == "user" else "assistant_message"
                return self._emit(pid, kind,
                                  text=_text(part.get("text")), message_id=_text(part.get("messageID")) or None,
                                  complete=True if _obj(part.get("time")).get("end") is not None else None)
        if typ in {"session.execution.succeeded", "session.execution.failed", "session.execution.interrupted", "session.error"}:
            return self._finish("done" if typ.endswith("succeeded") else "aborted", data.get("error"),
                                key=_text(raw.get("id")) or None)
        return []

    def _kimi(self, raw: dict) -> list[NativeUpdate]:
        typ, payload = raw.get("type"), _obj(raw.get("payload"))
        if typ not in {"transcript.reset", "transcript.ops"}:
            return []
        agent = _text(payload.get("agent_id"))
        seq = payload.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            previous = self._kimi_seqs.get(agent)
            if typ == "transcript.ops" and previous is not None and seq <= previous:
                return []
            # Grade filtering permits jumps in transport sequence. Frame offsets,
            # not contiguous envelope numbers, prove text continuity.
            self._kimi_seqs[agent] = seq
        if len(self._kimi_seqs) > self.limit:
            raise ValueError("too many Kimi agents")
        if typ == "transcript.reset":
            return self._kimi_reset(agent, _obj(payload.get("snapshot")))
        updates = []
        for op in payload.get("ops", []):
            op = _obj(op)
            if op.get("op") == "frame.upsert":
                updates += self._kimi_frame(agent, _obj(op.get("frame")), _text(op.get("turnId")))
            elif op.get("op") == "append":
                target = _obj(op.get("target"))
                key = f"kimi:{agent}:{target.get('frameId')}"
                old = self._values.get(key)
                if old is None or old.event is None:
                    raise ValueError("Kimi append baseline is missing")
                before = (old.event.text or "").encode("utf-16-le", errors="surrogatepass")
                chunk = _text(op.get("text")).encode("utf-16-le", errors="surrogatepass")
                offset = op.get("offset")
                if type(offset) is not int or offset < 0 or offset * 2 > len(before):
                    raise ValueError("Kimi append has a gap")
                overlap = min(len(chunk), len(before) - offset * 2)
                if before[offset * 2:offset * 2 + overlap] != chunk[:overlap]:
                    raise ValueError("Kimi append conflicts with baseline")
                if len(chunk) > overlap:
                    text = (before[:offset * 2] + chunk).decode("utf-16-le", errors="surrogatepass")
                    updates += self._emit(key, old.event.type, text=text, message_id=old.event.message_id,
                                          turn_id=old.event.turn_id, complete=False)
            elif op.get("op") == "reset":
                updates += self._kimi_reset(agent, _obj(op.get("snapshot")))
            elif op.get("op") == "items.remove":
                removed = op.get("ids", [])
                if not isinstance(removed, list) or any(not isinstance(item, str) for item in removed):
                    raise ValueError("Kimi items.remove has invalid ids")
                for key, value in list(self._values.items()):
                    if key.startswith(f"kimi:{agent}:") and value.event and value.event.turn_id in removed:
                        self._values.pop(key)
                        updates.append(NativeUpdate(key, None, operation="remove"))
            elif op.get("op") == "turn.upsert":
                turn = _obj(op.get("turn"))
                if turn.get("state") in {"completed", "failed", "cancelled"}:
                    updates += self._finish("done" if turn["state"] == "completed" else "aborted",
                                            turn.get("error"), turn_id=_text(turn.get("turnId")) or None)
        return updates

    def _kimi_reset(self, agent: str, snapshot: dict) -> list[NativeUpdate]:
        updates = []
        for key in list(self._values):
            if key.startswith(f"kimi:{agent}:"):
                self._values.pop(key)
                updates.append(NativeUpdate(key, None, operation="remove"))
        for item in snapshot.get("items", []):
            item = _obj(item)
            if item.get("kind") != "turn":
                continue
            for step in item.get("steps", []):
                for frame in _obj(step).get("frames", []):
                    updates += self._kimi_frame(agent, _obj(frame), _text(item.get("turnId")))
        return updates

    def _kimi_frame(self, agent: str, frame: dict, turn: str) -> list[NativeUpdate]:
        kind = frame.get("kind")
        if kind == "notice" and frame.get("level") == "error":
            return self._finish("aborted", frame.get("message"), turn_id=turn or None,
                                key=_text(frame.get("frameId")) or None)
        if kind not in {"text", "thinking"}:
            return []
        event_type = "thinking" if kind == "thinking" else "user_message" if frame.get("role") == "user" else "assistant_message"
        mid = _text(frame.get("frameId"))
        if not mid:
            raise ValueError("Kimi frame has no identity")
        return self._emit(f"kimi:{agent}:{mid}", event_type, text=_text(frame.get("text")),
                          message_id=mid, turn_id=turn or None, complete=None)
