//! OpenCode: SQLite `opencode.db` (v1 `session`/`message`, v2 `session_v2`/`session_message`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::jsonl::{self, Raw};
use crate::usage::{int_of, Counter, Normalized, Observation, Scope};

/// `<data>/opencode/opencode.db` with `<data>` = `XDG_DATA_HOME` or
/// `~/.local/share` on every OS, plus channel databases `opencode-<channel>.db`
/// beside it. `OPENCODE_DB` names the file (relative to the data dir) or an
/// absolute path; `:memory:` means no file. The legacy `OPENCODE_DATA_DIR`
/// (comma separated directories) honored by the Python parser is kept.
pub fn db_paths(home: &Path) -> Vec<PathBuf> {
    let base = std::env::var("XDG_DATA_HOME")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local").join("share"));
    let mut dirs = vec![base.join("opencode")];
    if let Ok(v) = std::env::var("OPENCODE_DATA_DIR") {
        dirs.extend(v.split(',').map(str::trim).filter(|s| !s.is_empty()).map(PathBuf::from));
    }
    let mut out: Vec<PathBuf> = Vec::new();
    match std::env::var("OPENCODE_DB").ok().filter(|v| !v.trim().is_empty()) {
        Some(v) if v == ":memory:" => {}
        Some(v) => {
            let p = PathBuf::from(&v);
            out.push(if p.is_absolute() { p } else { dirs[0].join(p) });
        }
        None => {
            for dir in &dirs {
                let Ok(entries) = std::fs::read_dir(dir) else { continue };
                for e in entries.flatten() {
                    let name = e.file_name();
                    let name = name.to_string_lossy();
                    if name == "opencode.db" || (name.starts_with("opencode-") && name.ends_with(".db")) {
                        out.push(e.path());
                    }
                }
            }
        }
    }
    out.retain(|p| p.is_file());
    out.sort();
    out.dedup();
    out
}

struct Row {
    session: String,
    id: String,
    time_created: Option<i64>,
    data: String,
    /// v2 `seq` (ordering key of the reference); v1 rows use time order.
    seq: Option<i64>,
}

/// `collect_opencode_observations` for one message row, reading only the
/// top-level fields it needs (`tokens`, `cost`, `modelID`, `model.id`, ids).
fn observe(row: &Row, ts: Option<f64>, out: &mut Vec<Observation>) {
    let data = row.data.as_bytes();
    let [tokens, cost, model_id, model, id, message_id, message_id2] =
        jsonl::top_level(data, [b"tokens", b"cost", b"modelID", b"model", b"id", b"message_id", b"messageId"]);
    if !data.starts_with(b"{") && !data.trim_ascii_start().starts_with(b"{") {
        return; // not a JSON object row
    }
    let tokens: Option<Value> = match tokens {
        Some(Raw::Obj(t)) => serde_json::from_slice(t).ok(),
        _ => None,
    };
    let cache = tokens.as_ref().and_then(|t| t.get("cache")).filter(|c| c.is_object());
    let input = tokens.as_ref().and_then(|t| int_of(t.get("input")));
    let output = tokens.as_ref().and_then(|t| int_of(t.get("output")));
    let reasoning = tokens.as_ref().and_then(|t| int_of(t.get("reasoning")));
    let cache_read = cache.and_then(|c| int_of(c.get("read")));
    let cache_write = cache.and_then(|c| int_of(c.get("write")));
    // Python: bools are not costs; non-negative numbers are.
    let cost = match cost {
        Some(Raw::Lit(l)) if l != b"true" && l != b"false" && l != b"null" => {
            std::str::from_utf8(l).ok().and_then(|v| v.parse::<f64>().ok()).filter(|v| *v >= 0.0)
        }
        _ => None,
    };
    let mut model_name = model_id.and_then(Raw::str).map(str::to_string);
    if let Some(Raw::Obj(m)) = model {
        if let Some(nested) = jsonl::top_level(m, [b"id"])[0].and_then(Raw::str) {
            if !nested.trim().is_empty() {
                model_name = Some(nested.trim().to_string());
            }
        }
    }
    let model_name = model_name.map(|m| m.trim().to_string()).filter(|m| !m.is_empty());
    if [input, output, cache_read, cache_write, reasoning].iter().all(Option::is_none)
        && cost.is_none()
        && model_name.is_none()
    {
        return;
    }
    // data.setdefault("id", row_id); then id or message_id or messageId (truthy).
    let pick = |raw: Option<Raw>| -> Option<Option<String>> {
        match raw {
            None => None,
            Some(Raw::Str(b"")) => None,
            Some(Raw::Str(v)) => Some(std::str::from_utf8(v).ok().map(str::to_string)),
            Some(Raw::Lit(b"null" | b"false" | b"0")) => None,
            Some(_) => Some(None), // truthy non-string: unknown record
        }
    };
    let mid: Option<String> = match id {
        None => Some(row.id.clone()),
        some => pick(some).or_else(|| pick(message_id)).or_else(|| pick(message_id2)).flatten(),
    };
    let in_parts: Vec<i64> = [input, cache_read, cache_write].iter().flatten().copied().collect();
    let out_parts: Vec<i64> = [output, reasoning].iter().flatten().copied().collect();
    let input_total = (!in_parts.is_empty()).then(|| in_parts.iter().sum());
    let output_total = (!out_parts.is_empty()).then(|| out_parts.iter().sum());
    let total = match (input_total, output_total) {
        (Some(i), Some(o)) => Some(i + o),
        _ => None,
    };
    out.push(Observation {
        record: String::new(),
        scope: Scope::Message,
        counter: Counter::Absolute,
        response_id: None,
        turn_id: None,
        message_id: mid.clone(),
        scope_id: mid,
        counter_name: None,
        model: model_name,
        normalized: Normalized {
            input_total,
            input_uncached: input,
            cache_read,
            cache_write,
            output_total,
            reasoning,
            total,
            reported_total: None,
            cost,
        },
        ts,
    });
}

fn table_set(conn: &Connection) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare("SELECT name FROM sqlite_master WHERE type='table'")?;
    let names = stmt.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
    Ok(names)
}

/// All listed (top-level, unarchived) sessions updated inside the window.
pub fn read(db: &Path, window: &Window) -> Vec<SessionStats> {
    let Ok(conn) = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)
    else {
        return Vec::new();
    };
    let _ = conn.busy_timeout(std::time::Duration::from_millis(500));
    let Ok(tables) = table_set(&conn) else { return Vec::new() };
    let has = |t: &str| tables.iter().any(|n| n == t);
    let start_ms = (window.start * 1000.0) as i64;
    let tx = conn.unchecked_transaction().ok();

    // (session id, directory, is_v2)
    let mut sessions: Vec<(String, Option<String>, bool)> = Vec::new();
    if has("session") {
        if let Ok(mut stmt) = conn.prepare(
            "SELECT id, directory FROM session WHERE parent_id IS NULL AND time_archived IS NULL AND time_updated >= ?1",
        ) {
            let mapped = stmt.query_map([start_ms], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)));
            if let Ok(rows) = mapped {
                sessions.extend(rows.flatten().map(|(id, dir)| (id, dir, false)));
            }
        }
    }
    if has("session_v2") {
        let sql = if has("session") {
            "SELECT id, directory FROM session_v2 s WHERE parent_id IS NULL AND time_archived IS NULL AND time_updated >= ?1 \
             AND NOT EXISTS (SELECT 1 FROM session old WHERE old.id = s.id)"
        } else {
            "SELECT id, directory FROM session_v2 WHERE parent_id IS NULL AND time_archived IS NULL AND time_updated >= ?1"
        };
        if let Ok(mut stmt) = conn.prepare(sql) {
            let mapped = stmt.query_map([start_ms], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)));
            if let Ok(rows) = mapped {
                sessions.extend(rows.flatten().map(|(id, dir)| (id, dir, true)));
            }
        }
    }

    drop(tx);
    let since = if window.full { i64::MIN } else { start_ms };
    let threads = rayon::current_num_threads().max(1);
    let chunk = sessions.len().div_ceil(threads * 2).max(1);
    let db = db.to_path_buf();
    sessions
        .par_chunks(chunk)
        .flat_map_iter(|part| read_chunk(&db, part, since, window))
        .collect()
}

/// Rows of one group of sessions on its own read-only connection.
fn read_chunk(db: &Path, sessions: &[(String, Option<String>, bool)], since: i64, window: &Window) -> Vec<SessionStats> {
    let mut rows: Vec<Row> = Vec::new();
    if let Ok(conn) = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX) {
        let _ = conn.busy_timeout(std::time::Duration::from_millis(500));
        // Memory-map the database: large message rows live on overflow pages,
        // which plain reads copy page by page through system calls.
        let _ = conn.execute_batch("PRAGMA mmap_size = 4294967296;");
        let fetch = |sql: &str, ids: &[&String], rows: &mut Vec<Row>| {
            if ids.is_empty() {
                return;
            }
            let marks = vec!["?"; ids.len()].join(",");
            let sql = sql.replace("{ids}", &marks);
            let Ok(mut stmt) = conn.prepare(&sql) else { return };
            let mut params: Vec<rusqlite::types::Value> =
                ids.iter().map(|s| rusqlite::types::Value::Text((*s).clone())).collect();
            params.push(rusqlite::types::Value::Integer(since));
            let mapped = stmt.query_map(rusqlite::params_from_iter(params), |r| {
                Ok(Row { session: r.get(0)?, id: r.get(1)?, time_created: r.get(2)?, data: r.get(3)?, seq: r.get(4)? })
            });
            if let Ok(it) = mapped {
                rows.extend(it.flatten());
            }
        };
        let v1: Vec<&String> = sessions.iter().filter(|s| !s.2).map(|s| &s.0).collect();
        let v2: Vec<&String> = sessions.iter().filter(|s| s.2).map(|s| &s.0).collect();
        fetch(
            "SELECT session_id, id, time_created, data, NULL FROM message WHERE session_id IN ({ids}) AND time_created >= ? \
             ORDER BY session_id, time_created ASC, id ASC",
            &v1,
            &mut rows,
        );
        fetch(
            // Index order (session, time): sorting the large rows by `seq` in SQL
            // spills them into a temporary B-tree; order by `seq` below instead.
            "SELECT session_id, id, time_created, data, seq FROM session_message WHERE session_id IN ({ids}) \
             AND time_created >= ? ORDER BY session_id, time_created ASC, id ASC",
            &v2,
            &mut rows,
        );
    }
    let mut by_session: HashMap<&str, Vec<&Row>> = HashMap::new();
    for r in &rows {
        by_session.entry(r.session.as_str()).or_default().push(r);
    }
    for list in by_session.values_mut() {
        if list.iter().any(|r| r.seq.is_some()) {
            list.sort_by(|a, b| a.seq.cmp(&b.seq).then_with(|| a.id.cmp(&b.id)));
        }
    }
    sessions
        .iter()
        .map(|(id, dir, _)| {
            let mut b = Builder::new(Runtime::Opencode, id.clone());
            b.cwd = dir.clone().filter(|d| !d.is_empty());
            b.excluded = cwd_exclusion(b.cwd.as_deref());
            if let Some(list) = by_session.get(id.as_str()) {
                for row in list {
                    let ts = row.time_created.map(|ms| ms as f64 / 1000.0);
                    if let Some(t) = ts {
                        b.activity.push(t);
                    }
                    observe(row, ts, &mut b.observations);
                    b.examined += row.data.len() as u64;
                }
            }
            b.finish(window)
        })
        .collect()
}
