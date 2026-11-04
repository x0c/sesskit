//! Claude Code: `~/.claude/projects/<project>/<session>.jsonl`.

use std::path::Path;

use serde_json::Value;

use crate::jsonl::{self, Raw};
use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::usage::{int_of, str_of, Counter, Normalized, Observation, Scope};

const TEAM_LEAD_PREFIX: &str = "<teammate-message teammate_id=\"team-lead\"";
/// Local-command wrappers that are not a human prompt (`_SKIP_PREFIXES`).
const SKIP_PREFIXES: [&str; 6] =
    ["<local-command", "<command-name>", "<command-message>", "<bash-input>", "<bash-stdout>", "<bash-stderr>"];

/// A real human prompt: non-meta user row with text outside local-command wrappers.
fn is_user_prompt(entry: &Value) -> bool {
    if entry.get("type").and_then(Value::as_str) != Some("user") || truthy(entry.get("isMeta")) {
        return false;
    }
    let text = text_of(entry.get("message").and_then(|m| m.get("content")));
    !text.is_empty() && !SKIP_PREFIXES.iter().any(|p| text.starts_with(p))
}
const HEAD_LINES: usize = 40;

fn entry_time(entry: &Value) -> Option<f64> {
    jsonl::parse_ts(entry.get("timestamp"))
        .or_else(|| jsonl::parse_ts(entry.get("snapshot").and_then(|s| s.get("timestamp"))))
}

fn text_of(content: Option<&Value>) -> String {
    match content {
        Some(Value::String(s)) => s.trim().to_string(),
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|i| {
                (i.get("type").and_then(Value::as_str) == Some("text"))
                    .then(|| i.get("text").and_then(Value::as_str))
                    .flatten()
            })
            .collect::<Vec<_>>()
            .join("\n")
            .trim()
            .to_string(),
        _ => String::new(),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::Null) | None => false,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    }
}

/// `_is_internal_claude_session` over the head entries.
fn is_internal(head: &[Value], own_id: &str) -> bool {
    for entry in head {
        if let Some(back) = entry.get("session_id").and_then(Value::as_str) {
            let back = back.trim();
            if !back.is_empty() && back != own_id {
                return false;
            }
        }
    }
    for entry in head {
        if truthy(entry.get("isSidechain")) {
            return true;
        }
        let kind = entry.get("type").and_then(Value::as_str);
        if kind == Some("agent-name") && truthy(entry.get("agentName")) {
            return true;
        }
        if kind != Some("user") || truthy(entry.get("isMeta")) {
            continue;
        }
        let text = text_of(entry.get("message").and_then(|m| m.get("content")));
        return text.starts_with(TEAM_LEAD_PREFIX);
    }
    false
}

fn observe(entry: &Value, out: &mut Vec<Observation>) {
    if entry.get("type").and_then(Value::as_str) == Some("cost-state") {
        return; // session rollup: never part of known_total
    }
    let Some(message) = entry.get("message").filter(|m| m.is_object()) else { return };
    let usage = message.get("usage").filter(|u| u.is_object());
    let model = message.get("model").and_then(Value::as_str);
    if usage.is_none() && model.is_none() {
        return;
    }
    let g = |k: &str| usage.and_then(|u| int_of(u.get(k)));
    let input = g("input_tokens");
    let output = g("output_tokens");
    let cache_read = g("cache_read_input_tokens");
    let cache_write = g("cache_creation_input_tokens");
    let reasoning = usage
        .and_then(|u| u.get("output_tokens_details"))
        .filter(|d| d.is_object())
        .and_then(|d| int_of(d.get("thinking_tokens")));
    let has_subdivisions = usage.is_some_and(|u| {
        u.get("cache_creation").is_some_and(|c| {
            int_of(c.get("ephemeral_1h_input_tokens")).is_some() || int_of(c.get("ephemeral_5m_input_tokens")).is_some()
        }) || u.get("server_tool_use").is_some_and(|s| {
            int_of(s.get("web_search_requests")).is_some() || int_of(s.get("web_fetch_requests")).is_some()
        })
    });
    if [input, output, cache_read, cache_write, reasoning].iter().all(Option::is_none)
        && !has_subdivisions
        && model.is_none()
    {
        return;
    }
    let id = str_of(message.get("id"));
    out.push(Observation {
        record: String::new(),
        scope: Scope::Message,
        counter: Counter::Absolute,
        response_id: id.clone(),
        turn_id: None,
        message_id: id.clone(),
        scope_id: id,
        counter_name: None,
        model: model.map(|m| m.trim().to_string()).filter(|m| !m.is_empty()),
        normalized: Normalized::inclusive(input, cache_read, cache_write, output, reasoning, None, None),
        ts: entry_time(entry),
    });
}

pub fn read(path: &Path, window: &Window) -> Option<SessionStats> {
    let own_id = path.file_stem()?.to_str()?.to_string();
    let bytes = std::fs::read(path).ok()?;
    let mut b = Builder::new(Runtime::Claude, own_id.clone());

    let mut has_prompt = false;
    let mut head: Vec<Value> = Vec::new();
    for line in jsonl::lines(&bytes, 0).take(HEAD_LINES) {
        if let Some(v) = jsonl::parse(line) {
            if b.cwd.is_none() {
                b.cwd = v.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
            }
            has_prompt |= is_user_prompt(&v);
            head.push(v);
        }
    }
    b.excluded = if is_internal(&head, &own_id) { Some("internal_agent") } else { cwd_exclusion(b.cwd.as_deref()) };
    drop(head);

    let from = if window.full { 0 } else { jsonl::window_offset(&bytes, window.start) };
    b.examined = (bytes.len() - from) as u64;
    for line in jsonl::lines(&bytes, from) {
        // Top-level fields only; full JSON decoding just for usage and prompt rows.
        let [ty, ts, meta, side, msg, snap, cwd] = jsonl::top_level(
            line,
            [b"type", b"timestamp", b"isMeta", b"isSidechain", b"message", b"snapshot", b"cwd"],
        );
        if b.cwd.is_none() {
            b.cwd = cwd.and_then(Raw::str).filter(|c| !c.is_empty()).map(str::to_string);
        }
        let kind = ty.and_then(Raw::str);
        let msg_obj = match msg {
            Some(Raw::Obj(m)) => Some(m),
            _ => None,
        };
        if matches!(kind, Some("user" | "assistant"))
            && msg_obj.is_some()
            && !meta.is_some_and(Raw::is_true)
            && !side.is_some_and(Raw::is_true)
        {
            let snap_ts = || match snap {
                Some(Raw::Obj(o)) => jsonl::raw_ts(jsonl::top_level(o, [b"timestamp"])[0]),
                _ => None,
            };
            if let Some(t) = jsonl::raw_ts(ts).or_else(snap_ts) {
                b.activity.push(t);
            }
        }
        let needs_usage = kind == Some("cost-state")
            || msg_obj.is_some_and(|m| jsonl::contains(m, b"\"usage\"") || jsonl::contains(m, b"\"model\""));
        let needs_prompt = !has_prompt && kind == Some("user");
        if needs_usage || needs_prompt {
            if let Some(entry) = jsonl::parse(line) {
                if needs_prompt {
                    has_prompt |= is_user_prompt(&entry);
                }
                if needs_usage {
                    observe(&entry, &mut b.observations);
                }
            }
        }
    }
    if b.excluded.is_none() && !has_prompt && from > 0 {
        // The first prompt may sit between the head and the window: check the
        // skipped region's user rows only (rare path).
        has_prompt = jsonl::lines(&bytes[..from], 0)
            .filter(|l| jsonl::top_level(l, [b"type"])[0].and_then(Raw::str) == Some("user"))
            .filter_map(jsonl::parse)
            .any(|e| is_user_prompt(&e));
    }
    if b.excluded.is_none() && !has_prompt {
        b.excluded = Some("empty_history");
    }
    Some(b.finish(window))
}
