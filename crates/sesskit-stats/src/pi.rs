//! Pi coding agent: `~/.pi/agent/sessions/<cwd-slug>/<time>_<id>.jsonl`.

use std::path::Path;

use serde_json::Value;

use std::collections::{HashMap, HashSet};

use crate::jsonl::{self, Raw};
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
        context_model: None,
        normalized: Normalized::inclusive(input, cache_read, cache_write, output, reasoning, total, cost),
        ts,
    });
}

fn raw_bytes<'a>(raw: Raw<'a>) -> Option<&'a [u8]> {
    match raw {
        Raw::Str(v) => Some(v),
        _ => None,
    }
}

/// Active branch (`parsers/pi.py::active_messages`): leaf = last id-bearing
/// entry, walk `parentId` to the root. Returns (branch message ids, branch has a
/// user message); `None` when some message lacks an id (v1 files: flat order).
fn active_branch(bytes: &[u8]) -> Option<(HashSet<Vec<u8>>, bool)> {
    let mut parent: HashMap<&[u8], Option<&[u8]>> = HashMap::new();
    let mut messages: HashSet<&[u8]> = HashSet::new();
    let mut users: HashSet<&[u8]> = HashSet::new();
    let mut last: Option<&[u8]> = None;
    for line in jsonl::lines(bytes, 0) {
        let [kind, id, parent_id] = jsonl::top_level(line, [b"type", b"id", b"parentId"]);
        let (kind, id, parent_id) = (
            kind.and_then(raw_bytes),
            id.and_then(raw_bytes),
            parent_id.and_then(raw_bytes),
        );
        let is_message = kind == Some(b"message".as_slice());
        let Some(id) = id else {
            if is_message {
                return None;
            }
            continue;
        };
        parent.insert(id, parent_id);
        if is_message {
            messages.insert(id);
            if let Some(Raw::Obj(m)) = jsonl::top_level(line, [b"message"])[0] {
                if jsonl::top_level(m, [b"role"])[0].and_then(Raw::str) == Some("user") {
                    users.insert(id);
                }
            }
        }
        last = Some(id);
    }
    let mut branch = HashSet::new();
    let mut has_user = false;
    let mut cur = last;
    while let Some(id) = cur {
        if !branch.insert(id.to_vec()) {
            break; // cycle guard
        }
        has_user |= users.contains(id);
        cur = parent.get(id).copied().flatten().filter(|p| parent.contains_key(p));
    }
    branch.retain(|id| messages.contains(id.as_slice()));
    Some((branch, has_user))
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
    let branch = active_branch(&bytes);
    if b.excluded.is_none() {
        let has_user = match &branch {
            Some((_, has_user)) => *has_user,
            None => jsonl::lines(&bytes, 0).any(|l| match jsonl::top_level(l, [b"message"])[0] {
                Some(Raw::Obj(m)) => jsonl::top_level(m, [b"role"])[0].and_then(Raw::str) == Some("user"),
                _ => false,
            }),
        };
        if !has_user {
            b.excluded = Some("empty_history");
        }
    }
    let from = if window.full { 0 } else { jsonl::window_offset(&bytes, window.start) };
    b.examined = (bytes.len() - from) as u64;
    for line in jsonl::lines(&bytes, from) {
        let [ty, id, ts, msg] = jsonl::top_level(line, [b"type", b"id", b"timestamp", b"message"]);
        let Some(Raw::Obj(m)) = msg else { continue };
        let [role, mts] = jsonl::top_level(m, [b"role", b"timestamp"]);
        let t = jsonl::raw_ts(ts).or_else(|| jsonl::raw_ts(mts));
        if let Some(t) = t {
            let on_branch = match (&branch, id.and_then(Raw::str)) {
                (Some((ids, _)), Some(id)) => ids.contains(id.as_bytes()),
                (Some(_), None) => false,
                (None, _) => true,
            };
            if on_branch && matches!(role.and_then(Raw::str), Some("user" | "assistant" | "toolResult")) {
                b.activity.push(t);
            }
        }
        if ty.and_then(Raw::str) == Some("message")
            && (jsonl::contains(m, b"\"usage\"") || jsonl::contains(m, b"\"model\""))
        {
            if let Some(item) = jsonl::parse(line) {
                observe(&item, t, &mut b.observations);
            }
        }
    }
    Some(b.finish(window))
}
