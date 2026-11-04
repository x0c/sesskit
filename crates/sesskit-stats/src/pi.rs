//! Pi coding agent: `~/.pi/agent/sessions/<cwd-slug>/<time>_<id>.jsonl`.

use std::path::Path;

use serde_json::Value;

use std::collections::{HashMap, HashSet};

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

/// Top-level string fields `type`, `id`, `parentId` of one JSON object line,
/// skipping nested values without decoding them (strings, objects, arrays).
fn top_fields(line: &[u8]) -> [Option<&[u8]>; 3] {
    let mut out = [None, None, None];
    let n = line.len();
    let mut i = match line.iter().position(|b| *b == b'{') {
        Some(p) => p + 1,
        None => return out,
    };
    let skip_ws = |i: &mut usize| {
        while *i < n && line[*i].is_ascii_whitespace() {
            *i += 1;
        }
    };
    // Returns the end index (exclusive) of a string starting at `i` (on the quote).
    let string_end = |mut i: usize| -> Option<usize> {
        i += 1;
        while i < n {
            match line[i] {
                b'\\' => i += 2,
                b'"' => return Some(i + 1),
                _ => i += 1,
            }
        }
        None
    };
    loop {
        skip_ws(&mut i);
        if i >= n || line[i] != b'"' {
            return out;
        }
        let Some(kend) = string_end(i) else { return out };
        let key = &line[i + 1..kend - 1];
        i = kend;
        skip_ws(&mut i);
        if i >= n || line[i] != b':' {
            return out;
        }
        i += 1;
        skip_ws(&mut i);
        if i >= n {
            return out;
        }
        let slot = match key {
            b"type" => Some(0),
            b"id" => Some(1),
            b"parentId" => Some(2),
            _ => None,
        };
        if line[i] == b'"' {
            let Some(vend) = string_end(i) else { return out };
            if let Some(k) = slot {
                out[k] = Some(&line[i + 1..vend - 1]);
            }
            i = vend;
        } else if line[i] == b'{' || line[i] == b'[' {
            let mut depth = 0i32;
            while i < n {
                match line[i] {
                    b'"' => match string_end(i) {
                        Some(e) => {
                            i = e;
                            continue;
                        }
                        None => return out,
                    },
                    b'{' | b'[' => depth += 1,
                    b'}' | b']' => {
                        depth -= 1;
                        if depth == 0 {
                            i += 1;
                            break;
                        }
                    }
                    _ => {}
                }
                i += 1;
            }
        } else {
            while i < n && line[i] != b',' && line[i] != b'}' {
                i += 1;
            }
        }
        skip_ws(&mut i);
        if i < n && line[i] == b',' {
            i += 1;
        } else {
            return out;
        }
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
        let [kind, id, parent_id] = top_fields(line);
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
            if memchr::memmem::find(line, b"\"role\":\"user\"").is_some() {
                users.insert(id);
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
            None => memchr::memmem::find(&bytes, b"\"role\":\"user\"").is_some(),
        };
        if !has_user {
            b.excluded = Some("empty_history");
        }
    }
    let from = if window.full { 0 } else { jsonl::window_offset(&bytes, window.start) };
    b.examined = (bytes.len() - from) as u64;
    for line in jsonl::lines(&bytes, from) {
        let Some(item) = jsonl::parse(line) else { continue };
        let message = item.get("message").filter(|m| m.is_object());
        let ts = jsonl::parse_ts(item.get("timestamp"))
            .or_else(|| jsonl::parse_ts(message.and_then(|m| m.get("timestamp"))));
        if let (Some(t), Some(m)) = (ts, message) {
            let on_branch = match (&branch, item.get("id").and_then(Value::as_str)) {
                (Some((ids, _)), Some(id)) => ids.contains(id.as_bytes()),
                (Some(_), None) => false,
                (None, _) => true,
            };
            if on_branch && matches!(m.get("role").and_then(Value::as_str), Some("user" | "assistant" | "toolResult")) {
                b.activity.push(t);
            }
        }
        observe(&item, ts, &mut b.observations);
    }
    Some(b.finish(window))
}

#[cfg(test)]
mod tests {
    use super::top_fields;

    #[test]
    fn top_level_fields_skip_nested_ids() {
        let line = br#"{"type":"custom_message","details":{"id":"nested","x":[{"id":"deep"}]},"content":"say \"id\":\"no\"","id":"top","parentId":"p1"}"#;
        assert_eq!(top_fields(line), [Some(&b"custom_message"[..]), Some(&b"top"[..]), Some(&b"p1"[..])]);
        let null_parent = br#"{"type":"model_change","id":"a","parentId":null,"n":3}"#;
        assert_eq!(top_fields(null_parent), [Some(&b"model_change"[..]), Some(&b"a"[..]), None]);
    }
}
