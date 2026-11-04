//! Pi coding agent: `~/.pi/agent/sessions/<cwd-slug>/<time>_<id>.jsonl`.

use std::path::Path;

use serde_json::Value;

use crate::jsonl;
use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::usage::{cost_of, int_of, str_of, Counter, Normalized, Observation, Scope};

fn observe(item: &Value, ts: Option<f64>, out: &mut Vec<Observation>) {
    if item.get("type").and_then(Value::as_str) != Some("message") {
        // Compaction / branch-summary / independent carriers are unknown-scope
        // records that never enter known_total.
        return;
    }
    let Some(message) = item.get("message").filter(|m| m.is_object()) else { return };
    let usage = message.get("usage").filter(|u| u.is_object());
    let model = message.get("model").and_then(Value::as_str);
    if usage.is_none() && model.is_none() {
        return;
    }
    let g = |k: &str| usage.and_then(|u| int_of(u.get(k)));
    let (input, output, cache_read, cache_write, reasoning, total) =
        (g("input"), g("output"), g("cacheRead"), g("cacheWrite"), g("reasoning"), g("totalTokens"));
    let cost = usage.and_then(|u| u.get("cost")).filter(|c| c.is_object()).and_then(|c| cost_of(c.get("total")));
    if [input, output, cache_read, cache_write, reasoning, total].iter().all(Option::is_none)
        && cost.is_none()
        && model.is_none()
    {
        return;
    }
    let assistant = message.get("role").and_then(Value::as_str) == Some("assistant");
    let id = str_of(item.get("id"));
    let response = str_of(message.get("responseId")).or_else(|| str_of(message.get("response_id")));
    out.push(Observation {
        record: String::new(),
        scope: if assistant { Scope::Message } else { Scope::Unknown },
        counter: Counter::Absolute,
        response_id: response,
        turn_id: None,
        message_id: id.clone(),
        scope_id: id,
        counter_name: None,
        model: model.map(|m| m.trim().to_string()).filter(|m| !m.is_empty()),
        normalized: Normalized::inclusive(input, cache_read, cache_write, output, reasoning, total, cost),
        ts,
    });
}

pub fn read(path: &Path, window: &Window) -> Option<SessionStats> {
    let bytes = std::fs::read(path).ok()?;
    let header = jsonl::lines(&bytes, 0).next().and_then(jsonl::parse)?;
    if header.get("type").and_then(Value::as_str) != Some("session") {
        return None;
    }
    let id = header.get("id").and_then(Value::as_str).filter(|s| !s.is_empty())?.to_string();
    let mut b = Builder::new(Runtime::Pi, id);
    b.cwd = header.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
    b.excluded = cwd_exclusion(b.cwd.as_deref());
    let from = if window.full { 0 } else { jsonl::window_offset(&bytes, window.start) };
    b.examined = (bytes.len() - from) as u64;
    for line in jsonl::lines(&bytes, from) {
        let Some(item) = jsonl::parse(line) else { continue };
        let message = item.get("message").filter(|m| m.is_object());
        let ts = jsonl::parse_ts(item.get("timestamp"))
            .or_else(|| jsonl::parse_ts(message.and_then(|m| m.get("timestamp"))));
        if let (Some(t), Some(m)) = (ts, message) {
            if matches!(m.get("role").and_then(Value::as_str), Some("user" | "assistant" | "toolResult")) {
                b.activity.push(t);
            }
        }
        observe(&item, ts, &mut b.observations);
    }
    Some(b.finish(window))
}
