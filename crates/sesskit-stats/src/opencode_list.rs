//! Compact OpenCode v2 listing facts read in one native database batch.
//!
//! Python keeps status/error/public-model interpretation. This module only
//! reduces large native rows to the small facts those existing helpers consume.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use rusqlite::{types::ValueRef, Connection, OpenFlags};
use serde::Serialize;
use serde_json::{json, Map, Value};

const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

struct Sha256 {
    state: [u32; 8],
    buf: [u8; 64],
    buf_len: usize,
    total_len: u64,
}

impl Sha256 {
    fn new() -> Self {
        Self {
            state: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
            ],
            buf: [0u8; 64],
            buf_len: 0,
            total_len: 0,
        }
    }

    fn compress(state: &mut [u32; 8], block: &[u8]) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            let j = i * 4;
            w[i] = u32::from_be_bytes([block[j], block[j + 1], block[j + 2], block[j + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = *state;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
        state[5] = state[5].wrapping_add(f);
        state[6] = state[6].wrapping_add(g);
        state[7] = state[7].wrapping_add(h);
    }

    fn update(&mut self, mut data: &[u8]) {
        self.total_len = self.total_len.wrapping_add(data.len() as u64);
        if self.buf_len > 0 {
            let take = (64 - self.buf_len).min(data.len());
            self.buf[self.buf_len..self.buf_len + take].copy_from_slice(&data[..take]);
            self.buf_len += take;
            data = &data[take..];
            if self.buf_len == 64 {
                Self::compress(&mut self.state, &self.buf);
                self.buf_len = 0;
            }
        }
        while data.len() >= 64 {
            Self::compress(&mut self.state, &data[..64]);
            data = &data[64..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    fn finalize_hex(mut self) -> String {
        let bit_len = self.total_len.wrapping_mul(8);
        self.update(&[0x80]);
        if self.buf_len > 56 {
            self.buf[self.buf_len..64].fill(0);
            Self::compress(&mut self.state, &self.buf);
            self.buf_len = 0;
        }
        self.buf[self.buf_len..56].fill(0);
        self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
        Self::compress(&mut self.state, &self.buf);
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut out = String::with_capacity(64);
        for word in self.state {
            for byte in word.to_be_bytes() {
                out.push(HEX[(byte >> 4) as usize] as char);
                out.push(HEX[(byte & 0x0f) as usize] as char);
            }
        }
        out
    }
}

/// Read-only list-signature fingerprint across OpenCode's four tracked tables.
pub fn content_fingerprint(db_path: &Path) -> Result<Vec<(String, u64, String)>, String> {
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| error.to_string())?;
    conn.busy_timeout(Duration::from_millis(500))
        .map_err(|error| error.to_string())?;
    let _ = conn.execute_batch("PRAGMA query_only = ON; PRAGMA mmap_size = 4294967296;");
    let tx = conn
        .unchecked_transaction()
        .map_err(|error| error.to_string())?;
    let len_fn = if rusqlite::version_number() >= 3_043_000 {
        "octet_length"
    } else {
        "length"
    };
    let queries = [
        (
            "session",
            "SELECT \"id\", \"directory\", \"title\", \"time_created\", \"time_updated\", \
             \"parent_id\", \"time_archived\" FROM \"session\" ORDER BY \"id\""
                .to_owned(),
            7usize,
        ),
        (
            "session_v2",
            "SELECT \"id\", \"directory\", \"title\", \"time_created\", \"time_updated\", \
             \"parent_id\", \"time_archived\" FROM \"session_v2\" ORDER BY \"id\""
                .to_owned(),
            7usize,
        ),
        (
            "message",
            format!(
                "SELECT \"id\", \"session_id\", \"time_created\", \"time_updated\", \
                 {len_fn}(\"data\") FROM \"message\" ORDER BY \"id\""
            ),
            5usize,
        ),
        (
            "session_message",
            format!(
                "SELECT \"id\", \"session_id\", \"type\", \"seq\", \"time_created\", \
                 \"time_updated\", {len_fn}(\"data\") FROM \"session_message\" ORDER BY \"id\""
            ),
            7usize,
        ),
    ];
    let mut out = Vec::with_capacity(queries.len());
    for (table, sql, ncols) in &queries {
        let mut stmt = tx.prepare(sql).map_err(|error| error.to_string())?;
        let mut rows = stmt.query([]).map_err(|error| error.to_string())?;
        let mut digest = Sha256::new();
        let mut row_count: u64 = 0;
        while let Some(row) = rows.next().map_err(|error| error.to_string())? {
            for col in 0..*ncols {
                match row.get_ref(col).map_err(|error| error.to_string())? {
                    ValueRef::Null => digest.update(&[0x00]),
                    ValueRef::Integer(v) => {
                        digest.update(&[0x01]);
                        digest.update(&v.to_le_bytes());
                    }
                    ValueRef::Real(v) => {
                        digest.update(&[0x02]);
                        digest.update(&v.to_bits().to_le_bytes());
                    }
                    ValueRef::Text(b) => {
                        digest.update(&[0x03]);
                        digest.update(&(b.len() as u64).to_le_bytes());
                        digest.update(b);
                    }
                    ValueRef::Blob(b) => {
                        digest.update(&[0x04]);
                        digest.update(&(b.len() as u64).to_le_bytes());
                        digest.update(b);
                    }
                }
            }
            digest.update(&[0xff]);
            row_count += 1;
        }
        out.push(((*table).to_owned(), row_count, digest.finalize_hex()));
    }
    drop(tx);
    Ok(out)
}

const SKIP_TYPES: [&str; 4] = ["system", "synthetic", "compaction", "idle"];

struct Row<'a> {
    id: &'a str,
    row_type: &'a str,
    seq: i64,
    data: &'a str,
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
    loop_count: u64,
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
            loop_count: 0,
        }
    }

    fn add(&mut self, row: Row<'_>) -> Result<(), String> {
        self.size_bytes += row.data_length;
        let data = parse_compact(row.data)?;
        match row.row_type {
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
                if is_valid_assistant_loop(&data) {
                    self.loop_count += 1;
                }
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
        let status = status_data(row.row_type, &data);
        if !SKIP_TYPES.contains(&row.row_type) {
            self.status_rows.clear();
        }
        self.status_rows.push((row.row_type.to_owned(), status));
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

pub fn is_valid_assistant_raw(raw: &str) -> bool {
    parse_compact(raw)
        .map(|data| is_valid_assistant_loop(&data))
        .unwrap_or(false)
}

fn is_valid_assistant_loop(data: &Value) -> bool {
    let has_content = data
        .get("content")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty());
    let has_non_error_finish = data
        .get("finish")
        .and_then(Value::as_str)
        .map(str::trim)
        .is_some_and(|finish| !finish.is_empty() && finish != "error")
        && !json_truthy(data.get("error"));
    has_content || has_non_error_finish
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
            let id = row
                .get_ref(0)
                .map_err(|error| error.to_string())?
                .as_str()
                .map_err(|error| error.to_string())?;
            let row_type = row
                .get_ref(1)
                .map_err(|error| error.to_string())?
                .as_str_or_null()
                .map_err(|error| error.to_string())?
                .unwrap_or_default();
            let data = row
                .get_ref(3)
                .map_err(|error| error.to_string())?
                .as_str()
                .map_err(|error| error.to_string())?;
            let record = Row {
                id,
                row_type,
                seq: row.get(2).map_err(|error| error.to_string())?,
                data,
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

    fn add_row(aggregate: &mut SessionAggregate, row_type: &str, seq: i64, data: Value) {
        let id = format!("m{seq}");
        let encoded = data.to_string();
        aggregate
            .add(Row {
                id: &id,
                row_type,
                seq,
                data_length: encoded.len() as i64,
                data: &encoded,
            })
            .unwrap();
    }

    #[test]
    fn aggregate_keeps_only_listing_facts() {
        let mut aggregate = SessionAggregate::new();
        add_row(&mut aggregate, "user", 1, json!({"text": "first", "ignored": "x"}));
        add_row(
            &mut aggregate,
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
        );
        add_row(
            &mut aggregate,
            "assistant",
            3,
            json!({
                "content": [{"type": "tool", "state": {"output": "large"}}],
                "error": {"message": "quota", "status": 429},
            }),
        );
        add_row(&mut aggregate, "idle", 4, json!({"outcome": "failed"}));
        let value = serde_json::to_value(aggregate).unwrap();
        assert_eq!(value["first_user"], "first");
        assert_eq!(
            value["last_agent_message"]["content"][0]["text"],
            " answer "
        );
        assert_eq!(value["last_anchor"], "m3:3");
        assert_eq!(value["status_rows"].as_array().unwrap().len(), 2);
        assert_eq!(value["assistant_errors"][0]["error"]["status"], 429);
        assert_eq!(value["loop_count"], 2);
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
            id: "broken",
            row_type: "assistant",
            seq: 1,
            data: "{",
            data_length: 1,
        });
        assert!(result.is_err());
    }

    #[test]
    fn sha256_matches_nist_vectors() {
        let h_empty = Sha256::new().finalize_hex();
        assert_eq!(
            h_empty,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        let mut h_abc = Sha256::new();
        h_abc.update(b"abc");
        assert_eq!(
            h_abc.finalize_hex(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let mut h_2blk = Sha256::new();
        h_2blk.update(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq");
        assert_eq!(
            h_2blk.finalize_hex(),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
    }

    #[test]
    fn content_fingerprint_tracks_mutations_and_ignores_part() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("sesskit_fp_{}_{nonce}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("opencode.db");
        let conn = Connection::open(&db).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER);
             CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT NOT NULL, title TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, parent_id TEXT, time_archived INTEGER);
             CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
             CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
             CREATE TABLE part (id TEXT PRIMARY KEY, data TEXT NOT NULL);
             INSERT INTO session_v2 VALUES ('s1', '/tmp', 't', 10, 20, NULL, NULL);
             INSERT INTO session_message VALUES ('m1', 's1', 'user', 0, 10, 20, '{\"text\":\"hi\"}');
             INSERT INTO part VALUES ('p1', 'abc');",
        )
        .unwrap();
        let fp1 = content_fingerprint(&db).unwrap();
        assert_eq!(fp1[1].1, 1);
        assert_eq!(fp1[3].1, 1);
        conn.execute("UPDATE part SET data = 'longer-part-payload'", [])
            .unwrap();
        let fp2 = content_fingerprint(&db).unwrap();
        assert_eq!(fp1, fp2);
        conn.execute(
            "UPDATE session_message SET data = '{\"text\":\"longer\"}' WHERE id = 'm1'",
            [],
        )
        .unwrap();
        let fp3 = content_fingerprint(&db).unwrap();
        assert_ne!(fp2, fp3);
        drop(conn);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
