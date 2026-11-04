//! Codex CLI: `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl`.

use std::path::Path;

use serde_json::Value;

use crate::jsonl;
use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::usage::{int_of, trimmed, Counter, Normalized, Observation, Scope};

/// Trailing UUID of `rollout-...-<uuid>.jsonl` (`_extract_uuid_from_filename`).
fn uuid_of(path: &Path) -> Option<String> {
    let stem = path.file_stem()?.to_str()?;
    (stem.len() >= 36).then(|| stem[stem.len() - 36..].to_string()).filter(|u| {
        u.chars().filter(|c| *c == '-').count() == 4 && u.chars().all(|c| c == '-' || c.is_ascii_hexdigit())
    })
}

fn turn_id(payload: &Value) -> Option<String> {
    ["turn_id", "turnId", "turnID"].iter().find_map(|k| trimmed(payload.get(*k)))
}

#[allow(clippy::too_many_arguments)]
fn add(
    out: &mut Vec<Observation>,
    block: &Value,
    model: Option<&Value>,
    scope: Scope,
    counter: Counter,
    response_id: Option<String>,
    turn: Option<String>,
    thread: Option<String>,
    counter_name: Option<&str>,
    ts: Option<f64>,
) {
    let g = |k: &str| int_of(block.get(k));
    let (input, output, cache_read, cache_write, reasoning, total) = (
        g("input_tokens"),
        g("output_tokens"),
        g("cached_input_tokens"),
        g("cache_write_input_tokens"),
        g("reasoning_output_tokens"),
        g("total_tokens"),
    );
    let model = model.and_then(Value::as_str);
    if [input, output, cache_read, cache_write, reasoning, total].iter().all(Option::is_none) && model.is_none() {
        return;
    }
    let counter_name = counter_name
        .unwrap_or(if counter == Counter::Cumulative { "turn_token_usage" } else { "usage" })
        .to_string();
    out.push(Observation {
        record: String::new(),
        scope,
        counter,
        scope_id: thread.or_else(|| response_id.clone()).or_else(|| turn.clone()),
        response_id,
        turn_id: turn,
        message_id: None,
        counter_name: Some(counter_name),
        model: model.map(|m| m.trim().to_string()).filter(|m| !m.is_empty()),
        normalized: Normalized {
            input_total: input,
            input_uncached: None,
            cache_read,
            cache_write,
            output_total: output,
            reasoning,
            total: None,
            reported_total: total,
            cost: None,
        },
        ts,
    });
}

fn observe(entry: &Value, ts: Option<f64>, out: &mut Vec<Observation>) {
    let kind = entry.get("type").and_then(Value::as_str).unwrap_or("");
    let Some(payload) = entry.get("payload").filter(|p| p.is_object()) else { return };
    if kind == "token_usage_record" {
        let turn = turn_id(payload);
        let thread = trimmed(payload.get("thread_id"));
        let response = trimmed(payload.get("response_id"));
        let model = payload.get("model");
        if let Some(block) = payload.get("turn_token_usage").filter(|b| b.is_object()) {
            add(out, block, model, Scope::Turn, Counter::Cumulative, response.clone(), turn.clone(), None, None, ts);
        }
        if let Some(block) = payload.get("usage").filter(|b| b.is_object()) {
            add(out, block, model, Scope::Call, Counter::Absolute, response.clone(), turn.clone(), None, None, ts);
        }
        if let Some(block) = payload.get("thread_token_usage").filter(|b| b.is_object()) {
            add(out, block, model, Scope::Session, Counter::Cumulative, None, None, thread, Some("thread_token_usage"), ts);
        }
        return;
    }
    if kind == "event_msg" {
        let Some(info) = payload.get("info").filter(|i| i.is_object()) else { return };
        // Model lookup follows `{**payload, **info}`.
        let model = info.get("model").or_else(|| payload.get("model"));
        let turn = turn_id(payload).or_else(|| turn_id(info));
        for key in ["last_token_usage", "last_usage", "total_token_usage", "total_usage"] {
            if let Some(block) = info.get(key).filter(|b| b.is_object()) {
                let total = key.starts_with("total");
                add(
                    out,
                    block,
                    model,
                    if total { Scope::Turn } else { Scope::Call },
                    if total { Counter::Cumulative } else { Counter::Absolute },
                    None,
                    turn.clone(),
                    None,
                    None,
                    ts,
                );
            }
        }
    }
}

fn is_activity(entry: &Value) -> bool {
    match entry.get("type").and_then(Value::as_str) {
        Some("response_item") => true,
        Some("event_msg") => matches!(
            entry.get("payload").and_then(|p| p.get("type")).and_then(Value::as_str),
            Some("user_message" | "agent_message")
        ),
        _ => false,
    }
}

pub fn read(path: &Path, window: &Window) -> Option<SessionStats> {
    let id = uuid_of(path)?;
    let bytes = std::fs::read(path).ok()?;
    let mut b = Builder::new(Runtime::Codex, id);
    let mut thread_source: Option<String> = None;
    for line in jsonl::lines(&bytes, 0).take(20) {
        let Some(entry) = jsonl::parse(line) else { continue };
        if entry.get("type").and_then(Value::as_str) == Some("session_meta") {
            let payload = entry.get("payload");
            b.cwd = payload.and_then(|p| p.get("cwd")).and_then(Value::as_str).map(str::to_string);
            thread_source = payload.and_then(|p| p.get("thread_source")).and_then(Value::as_str).map(str::to_string);
            break;
        }
    }
    b.excluded = if thread_source.as_deref() == Some("subagent") {
        Some("internal_agent")
    } else {
        cwd_exclusion(b.cwd.as_deref())
    };
    let from = if window.full { 0 } else { jsonl::window_offset(&bytes, window.start) };
    b.examined = (bytes.len() - from) as u64;
    for line in jsonl::lines(&bytes, from) {
        let Some(entry) = jsonl::parse(line) else { continue };
        let ts = jsonl::parse_ts(entry.get("timestamp"));
        if is_activity(&entry) {
            if let Some(t) = ts {
                b.activity.push(t);
            }
        }
        observe(&entry, ts, &mut b.observations);
    }
    Some(b.finish(window))
}
