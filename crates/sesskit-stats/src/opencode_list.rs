//! Compact OpenCode v2 listing facts read in one native database batch.
//!
//! Python keeps status/error/public-model interpretation. This module only
//! reduces large native rows to the small facts those existing helpers consume.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use serde_json::{json, Map, Value};

const SKIP_TYPES: [&str; 4] = ["system", "synthetic", "compaction", "idle"];

struct Row {
    id: String,
    row_type: String,
    seq: i64,
    data: String,
    data_length: i64,
}

#[derive(Serialize)]
struct SessionAggregate {
    has_user: bool,
    first_user: String,
    last_user: String,
    goal_rows: Vec<Value>,
    last_agent_message: Value,
    status_rows: Vec<(String, Value)>,
    assistant_errors: Vec<Value>,
    last_anchor: String,
    size_bytes: i64,
}

impl SessionAggregate {
    fn new() -> Self {
        Self {
            has_user: false,
            first_user: String::new(),
            last_user: String::new(),
            goal_rows: Vec::new(),
            last_agent_message: json!({}),
            status_rows: Vec::new(),
            assistant_errors: Vec::new(),
            last_anchor: String::new(),
            size_bytes: 0,
        }
    }

    fn add(&mut self, row: Row) -> Result<(), String> {
        self.size_bytes += row.data_length;
        let data = parse_compact(&row.data)?;
        match row.row_type.as_str() {
            "user" => {
                let text = string_field(&data, "text");
                if !self.has_user {
                    self.first_user = text.clone();
                }
                self.last_user = text;
                self.has_user = true;
            }
            "assistant" => {
                self.last_anchor = format!("{}:{}", row.id, row.seq);
                if let Some(content) = last_text_content(&data) {
                    self.last_agent_message = content;
                }
                if json_truthy(data.get("error")) {
                    self.assistant_errors
                        .push(json!({"error": data.get("error").cloned()}));
                }
            }
            "synthetic"
                if data
                    .get("metadata")
                    .and_then(|m| m.get("source"))
                    .and_then(Value::as_str)
                    == Some("goal") =>
            {
                self.goal_rows.push(json!({
                    "description": string_value(&data, "description"),
                    "text": string_value(&data, "text"),
                    "metadata": {"source": "goal"},
                }));
            }
            _ => {}
        }
        let status = status_data(&row.row_type, &data);
        if !SKIP_TYPES.contains(&row.row_type.as_str()) {
            self.status_rows.clear();
        }
        self.status_rows.push((row.row_type, status));
        Ok(())
    }
}

fn parse_compact(raw: &str) -> Result<Value, String> {
    let projected = crate::projection::project_json(raw, "opencode_list")
        .ok_or_else(|| "unsupported OpenCode JSON row".to_owned())?;
    let value = serde_json::from_str::<Value>(&projected).map_err(|error| error.to_string())?;
    value
        .is_object()
        .then_some(value)
        .ok_or_else(|| "non-object OpenCode JSON row".to_owned())
}

fn string_field(data: &Value, key: &str) -> String {
    data.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn string_value(data: &Value, key: &str) -> Value {
    data.get(key)
        .filter(|value| value.is_string())
        .cloned()
        .unwrap_or(Value::Null)
}

fn json_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64() != Some(0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

// Python str.isspace(), which str.strip() uses, includes the four information
// separators that Rust's Unicode White_Space property does not. Keep candidate
// selection aligned; Python still performs the final text stripping/joining.
fn python_whitespace(character: char) -> bool {
    matches!(character,
        '\u{0009}'..='\u{000d}' | '\u{001c}'..='\u{0020}' | '\u{0085}' | '\u{00a0}'
        | '\u{1680}' | '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}'
        | '\u{202f}' | '\u{205f}' | '\u{3000}'
    )
}

fn last_text_content(data: &Value) -> Option<Value> {
    let content = data.get("content")?.as_array()?;
    let mut texts = Vec::new();
    let mut non_empty = false;
    for item in content {
        if item.get("type").and_then(Value::as_str) != Some("text") {
            continue;
        }
        let Some(text) = item.get("text").and_then(Value::as_str) else {
            continue;
        };
        non_empty |= !text.trim_matches(python_whitespace).is_empty();
        texts.push(json!({"type": "text", "text": text}));
    }
    non_empty.then(|| json!({"content": texts}))
}

fn status_data(row_type: &str, data: &Value) -> Value {
    let mut out = Map::new();
    if row_type == "assistant" {
        out.insert("finish".to_owned(), string_value(data, "finish"));
        if json_truthy(data.get("error")) {
            out.insert(
                "error".to_owned(),
                data.get("error").cloned().unwrap_or(Value::Null),
            );
        }
    } else if row_type == "idle" {
        out.insert("outcome".to_owned(), string_value(data, "outcome"));
    }
    Value::Object(out)
}

/// Return compact listing facts keyed by requested session id. Any database or
/// encoding error fails the whole native batch so Python can use its SQL path.
pub fn project_json(db_path: &Path, session_ids: &[String]) -> Result<String, String> {
    let mut aggregates: BTreeMap<String, SessionAggregate> = session_ids
        .iter()
        .map(|id| (id.clone(), SessionAggregate::new()))
        .collect();
    if session_ids.is_empty() {
        return serde_json::to_string(&aggregates).map_err(|error| error.to_string());
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| error.to_string())?;
    conn.busy_timeout(Duration::from_millis(500))
        .map_err(|error| error.to_string())?;
    let _ = conn.execute_batch("PRAGMA mmap_size = 4294967296;");
    let tx = conn
        .unchecked_transaction()
        .map_err(|error| error.to_string())?;
    // Reuse the session/seq index instead of sorting large message bodies into
    // a temporary B-tree for one giant IN (...) query.
    let mut statement = tx
        .prepare(
            "SELECT id, type, seq, data, LENGTH(data) FROM session_message \
         WHERE session_id = ?1 ORDER BY seq ASC, time_created ASC, id ASC",
        )
        .map_err(|error| error.to_string())?;
    for session_id in session_ids {
        let aggregate = aggregates
            .get_mut(session_id)
            .ok_or_else(|| "missing requested OpenCode aggregate".to_owned())?;
        let mut rows = statement
            .query([session_id])
            .map_err(|error| error.to_string())?;
        while let Some(row) = rows.next().map_err(|error| error.to_string())? {
            let record = Row {
                id: row.get(0).map_err(|error| error.to_string())?,
                row_type: row
                    .get::<_, Option<String>>(1)
                    .map_err(|error| error.to_string())?
                    .unwrap_or_default(),
                seq: row.get(2).map_err(|error| error.to_string())?,
                data: row.get(3).map_err(|error| error.to_string())?,
                data_length: row.get(4).map_err(|error| error.to_string())?,
            };
            aggregate.add(record)?;
        }
    }
    drop(statement);
    drop(tx);
    serde_json::to_string(&aggregates).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(row_type: &str, seq: i64, data: Value) -> Row {
        let data = data.to_string();
        Row {
            id: format!("m{seq}"),
            row_type: row_type.to_owned(),
            seq,
            data_length: data.len() as i64,
            data,
        }
    }

    #[test]
    fn aggregate_keeps_only_listing_facts() {
        let mut aggregate = SessionAggregate::new();
        aggregate
            .add(row("user", 1, json!({"text": "first", "ignored": "x"})))
            .unwrap();
        aggregate
            .add(row(
                "assistant",
                2,
                json!({
                    "content": [
                        {"type": "reasoning", "reasoningEncryptedContent": "secret"},
                        {"type": "text", "text": " answer "},
                        {"type": "tool", "state": {"input": "large"}},
                    ],
                    "finish": "tool-calls",
                }),
            ))
            .unwrap();
        aggregate
            .add(row(
                "assistant",
                3,
                json!({
                    "content": [{"type": "tool", "state": {"output": "large"}}],
                    "error": {"message": "quota", "status": 429},
                }),
            ))
            .unwrap();
        aggregate
            .add(row("idle", 4, json!({"outcome": "failed"})))
            .unwrap();
        let value = serde_json::to_value(aggregate).unwrap();
        assert_eq!(value["first_user"], "first");
        assert_eq!(
            value["last_agent_message"]["content"][0]["text"],
            " answer "
        );
        assert_eq!(value["last_anchor"], "m3:3");
        assert_eq!(value["status_rows"].as_array().unwrap().len(), 2);
        assert_eq!(value["assistant_errors"][0]["error"]["status"], 429);
        let encoded = value.to_string();
        assert!(!encoded.contains("large"));
        assert!(!encoded.contains("secret"));
    }

    #[test]
    fn candidate_whitespace_matches_python_special_cases() {
        for character in ['\u{001c}', '\u{001f}', '\u{0085}', '\u{2000}', '\u{3000}'] {
            assert!(python_whitespace(character));
        }
        for character in ['\u{200b}', '\u{200e}', '\u{feff}', 'x'] {
            assert!(!python_whitespace(character));
        }
    }

    #[test]
    fn malformed_rows_request_python_fallback() {
        let mut aggregate = SessionAggregate::new();
        let result = aggregate.add(Row {
            id: "broken".to_owned(),
            row_type: "assistant".to_owned(),
            seq: 1,
            data: "{".to_owned(),
            data_length: 1,
        });
        assert!(result.is_err());
    }
}
