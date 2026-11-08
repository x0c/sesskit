//! Codex CLI: `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl`.

use std::path::Path;

use serde_json::Value;

use crate::jsonl::{self, Raw};
use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::usage::{int_of, trimmed, Counter, Normalized, Observation, Scope};

/// Thread UUID of `rollout-<ts>-<thread>[_<rollout>].jsonl`: after a revert the
/// file name gains `_<rollout id>`, while the thread id stays the session id.
fn uuid_of(path: &Path) -> Option<String> {
    let stem = path.file_stem()?.to_str()?;
    let is_uuid = |u: &str| {
        u.len() == 36 && u.chars().filter(|c| *c == '-').count() == 4 && u.chars().all(|c| c == '-' || c.is_ascii_hexdigit())
    };
    let tail = |s: &str| (s.len() >= 36).then(|| s[s.len() - 36..].to_string()).filter(|u| is_uuid(u));
    if let Some((head, rollout)) = stem.rsplit_once('_') {
        if is_uuid(rollout) {
            if let Some(thread) = tail(head) {
                return Some(thread);
            }
        }
    }
    tail(stem)
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
        context_model: None,
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


/// Real user input (`parsers/codex.py::user_message_text`): legacy
/// `event_msg.user_message`, or a `response_item` user message whose text is not
/// injected framework context.
fn is_user_prompt(entry: &Value) -> bool {
    let Some(payload) = entry.get("payload") else { return false };
    match entry.get("type").and_then(Value::as_str) {
        Some("event_msg") => {
            payload.get("type").and_then(Value::as_str) == Some("user_message")
                && payload.get("message").and_then(Value::as_str).is_some_and(|m| !m.trim().is_empty())
        }
        Some("response_item") => {
            if payload.get("type").and_then(Value::as_str) != Some("message")
                || payload.get("role").and_then(Value::as_str) != Some("user")
            {
                return false;
            }
            let Some(items) = payload.get("content").and_then(Value::as_array) else { return false };
            let text = items
                .iter()
                .filter(|i| matches!(i.get("type").and_then(Value::as_str), Some("input_text" | "output_text")))
                .filter_map(|i| i.get("text").and_then(Value::as_str).map(str::trim))
                .filter(|t| !t.is_empty())
                .collect::<Vec<_>>()
                .join("\n");
            !text.is_empty() && !text.starts_with("# AGENTS.md instructions") && !text.starts_with("<environment_context>")
        }
        _ => false,
    }
}

/// Configured model of a `turn_context` entry: `payload.model`, else
/// `payload.collaboration_mode.settings.model`.
fn context_model(entry: &Value) -> Option<String> {
    let payload = entry.get("payload")?;
    trimmed(payload.get("model")).or_else(|| {
        trimmed(payload.get("collaboration_mode").and_then(|c| c.get("settings")).and_then(|s| s.get("model")))
    })
}

/// Latest `turn_context` model in the pruned prefix `bytes[..from]`, searched
/// backwards so in-window usage before the first in-window turn keeps a model.
fn context_before(bytes: &[u8], from: usize) -> Option<String> {
    let finder = memchr::memmem::FinderRev::new(b"turn_context");
    let mut end = from;
    while let Some(pos) = finder.rfind(&bytes[..end]) {
        let start = memchr::memrchr(b'\n', &bytes[..pos]).map_or(0, |i| i + 1);
        let stop = memchr::memchr(b'\n', &bytes[pos..end]).map_or(end, |i| pos + i);
        let model = jsonl::parse(&bytes[start..stop])
            .filter(|e| e.get("type").and_then(Value::as_str) == Some("turn_context"))
            .and_then(|e| context_model(&e));
        if model.is_some() {
            return model;
        }
        if start == 0 {
            break;
        }
        end = start;
    }
    None
}

/// When pruning skipped the start of the file, look for the first prompt in the
/// skipped region (user rows only).
fn skipped_has_prompt(bytes: &[u8], from: usize) -> bool {
    from > 0
        && jsonl::lines(&bytes[..from], 0)
            .filter(|l| jsonl::contains(l, b"user"))
            .filter_map(jsonl::parse)
            .any(|e| is_user_prompt(&e))
}

pub fn read(path: &Path, window: &Window) -> Option<SessionStats> {
    let id = uuid_of(path)?;
    let bytes = std::fs::read(path).ok()?;
    let mut b = Builder::new(Runtime::Codex, id);
    let mut thread_source: Option<String> = None;
    let mut has_prompt = false;
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
    let mut context = if from > 0 { context_before(&bytes, from) } else { None };
    for line in jsonl::lines(&bytes, from) {
        let [ty, ts, payload] = jsonl::top_level(line, [b"type", b"timestamp", b"payload"]);
        let kind = ty.and_then(Raw::str);
        let Some(Raw::Obj(p)) = payload else { continue };
        let ptype = jsonl::top_level(p, [b"type"])[0].and_then(Raw::str);
        let t = jsonl::raw_ts(ts);
        if kind == Some("turn_context") {
            if let Some(model) = jsonl::parse(line).and_then(|e| context_model(&e)) {
                context = Some(model);
            }
        }
        let activity = match kind {
            Some("response_item") => true,
            Some("event_msg") => matches!(ptype, Some("user_message" | "agent_message")),
            _ => false,
        };
        if activity {
            if let Some(t) = t {
                b.activity.push(t);
            }
        }
        let needs_usage = kind == Some("token_usage_record")
            || (kind == Some("event_msg") && matches!(jsonl::top_level(p, [b"info"])[0], Some(Raw::Obj(_))));
        let needs_prompt = !has_prompt
            && ((kind == Some("event_msg") && ptype == Some("user_message"))
                || (kind == Some("response_item")
                    && jsonl::top_level(p, [b"role"])[0].and_then(Raw::str) == Some("user")));
        if needs_usage || needs_prompt {
            if let Some(entry) = jsonl::parse(line) {
                if needs_prompt {
                    has_prompt |= is_user_prompt(&entry);
                }
                if needs_usage {
                    let before = b.observations.len();
                    observe(&entry, t, &mut b.observations);
                    for obs in &mut b.observations[before..] {
                        if obs.model.is_none() {
                            obs.context_model = context.clone();
                        }
                    }
                }
            }
        }
    }
    if b.excluded.is_none() && !has_prompt && !skipped_has_prompt(&bytes, from) {
        b.excluded = Some("empty_history");
    }
    Some(b.finish(window))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rollout(dir: &Path) -> std::path::PathBuf {
        let cwd = dir.display().to_string().replace('\\', "\\\\");
        // Native token_usage_record rows carry ids but no model.
        let usage = |ts: &str, input: i64| {
            format!(
                r#"{{"timestamp":"{ts}","type":"token_usage_record","payload":{{"response_id":"r{input}","turn_id":"t{input}","thread_id":"th","usage":{{"input_tokens":{input},"output_tokens":10,"total_tokens":{}}}}}}}"#,
                input + 10
            )
        };
        let lines = [
            format!(r#"{{"timestamp":"2026-11-08T00:00:00.000Z","type":"session_meta","payload":{{"cwd":"{cwd}"}}}}"#),
            r#"{"timestamp":"2026-11-08T00:00:01.000Z","type":"event_msg","payload":{"type":"user_message","message":"hi"}}"#.to_string(),
            r#"{"timestamp":"2026-11-08T00:00:02.000Z","type":"turn_context","payload":{"model":"gpt-a","effort":"high"}}"#.to_string(),
            usage("2026-11-08T00:00:03.000Z", 100),
            usage("2026-11-08T01:00:00.000Z", 200),
            r#"{"timestamp":"2026-11-08T02:00:00.000Z","type":"turn_context","payload":{"collaboration_mode":{"settings":{"model":"gpt-b"}}}}"#.to_string(),
            usage("2026-11-08T02:00:01.000Z", 300),
        ];
        let path = dir.join("rollout-2026-11-08T00-00-00-01a1b98f-2a90-74c3-be60-a751f53571bd.jsonl");
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();
        path
    }

    fn models(s: &SessionStats) -> Vec<(Option<&str>, i64)> {
        s.window_models.iter().map(|(m, t)| (m.as_deref(), *t)).collect()
    }

    #[test]
    fn token_counts_take_the_preceding_turn_context_model() {
        let dir = std::env::temp_dir().join(format!("sesskit-codex-models-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = rollout(&dir);
        let full = read(&path, &Window { start: 0.0, end: f64::MAX, full: true }).unwrap();
        assert_eq!(models(&full), vec![(Some("gpt-a"), 320), (Some("gpt-b"), 310)]);
        assert_eq!(full.window_models.iter().map(|m| m.1).sum::<i64>(), full.window_tokens);
        // A window starting after the first turn_context still attributes its
        // first in-window usage to gpt-a (found in the pruned prefix).
        let start = 1_794_097_800.0; // 2026-11-08T00:30:00Z
        let pruned = read(&path, &Window { start, end: f64::MAX, full: false }).unwrap();
        assert_eq!(models(&pruned), vec![(Some("gpt-b"), 310), (Some("gpt-a"), 210)]);
        assert_eq!(pruned.window_tokens, 520);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
