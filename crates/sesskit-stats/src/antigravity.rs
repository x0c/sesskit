//! Antigravity (`agy` / Antigravity IDE) session transcripts and summaries.
//!
//! Real-row token semantics verified against live `~/.gemini/antigravity-cli/`
//! (`v1.5.8`) `transcript_full.jsonl` logs:
//! - Consecutive `PLANNER_RESPONSE` rows show `input_tokens` equals only the
//!   newly uncached input delta while `cache_read_tokens` tracks the prefix
//!   carried from prior turns (e.g. step 2 `in=22438, cache=0, out=612` ->
//!   step 4 `in=2946, cache=22439` -> step 9 `in=6018, cache=25386`, where
//!   each step's `cache_read_tokens` equals previous `in + cache_read + 1`).
//!   Therefore `input_tokens` is UNCACHED input, so `Normalized::inclusive`
//!   sums `input_tokens + cache_read_tokens` into `input_total`.
//! - `CHECKPOINT` compaction rows (e.g. step 101 `# Resuming from a compaction`
//!   carrying `in=5027, cache=253022, out=7975` between step 99
//!   `PLANNER_RESPONSE` `in=3608, cache=249413` and step 102 `in=32288, cache=0`)
//!   are independent LLM summarization calls, not cumulative turn rollups.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use crate::jsonl;
use crate::session::{cwd_exclusion, Builder, Runtime, SessionStats, Window};
use crate::usage::{Counter, Normalized, Observation, Scope};

pub const ROOT_NAMES: &[&str] = &[
    ".gemini/antigravity",
    ".gemini/antigravity-cli",
    ".gemini/antigravity-ide",
    ".gemini/antigravity-backup",
    ".gemini/antigravity-acp",
    ".config/antigravity",
];

#[derive(Default, Debug, Clone)]
pub struct SummaryMeta {
    pub cwd: Option<String>,
    pub is_subagent: bool,
}

struct ParsedRow {
    step_index: Option<i64>,
    line_idx: usize,
    kind: String,
    source: Option<String>,
    ts: Option<f64>,
    user_prompt: bool,
    model_selection: Option<String>,
    input_tokens: Option<i64>,
    cache_read_tokens: Option<i64>,
    output_tokens: Option<i64>,
}

fn mtime(path: &Path) -> Option<f64> {
    let m = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(m.duration_since(SystemTime::UNIX_EPOCH).ok()?.as_secs_f64())
}

/// Discover Antigravity transcripts modified at or after `start` (epoch secs),
/// preferring `transcript_full.jsonl` over `transcript.jsonl` per conversation.
pub fn discover(home: &Path, start: f64) -> Vec<PathBuf> {
    let mut best: HashMap<String, (PathBuf, bool, f64)> = HashMap::new();
    for root in ROOT_NAMES {
        let brain = home.join(root).join("brain");
        let Ok(entries) = std::fs::read_dir(&brain) else { continue };
        for entry in entries.flatten() {
            let conv_dir = entry.path();
            if !entry.file_type().is_ok_and(|ft| ft.is_dir()) {
                continue;
            }
            let Some(cid) = entry.file_name().to_str().map(str::to_string) else { continue };
            let logs = conv_dir.join(".system_generated").join("logs");
            let full = logs.join("transcript_full.jsonl");
            let slim = logs.join("transcript.jsonl");
            let (chosen, is_full) = if full.is_file() {
                (full, true)
            } else if slim.is_file() {
                (slim, false)
            } else {
                continue;
            };
            let Some(file_mtime) = mtime(&chosen) else { continue };
            if file_mtime < start {
                continue;
            }
            match best.get(&cid) {
                None => {
                    best.insert(cid, (chosen, is_full, file_mtime));
                }
                Some((prev_path, prev_full, prev_mtime)) => {
                    let take = (is_full && !*prev_full)
                        || (is_full == *prev_full
                            && (file_mtime > *prev_mtime
                                || (file_mtime == *prev_mtime && chosen < *prev_path)));
                    if take {
                        best.insert(cid, (chosen, is_full, file_mtime));
                    }
                }
            }
        }
    }
    let mut out: Vec<PathBuf> = best.into_values().map(|(p, _, _)| p).collect();
    out.sort();
    out
}

fn conv_id_and_root(path: &Path) -> Option<(String, Option<PathBuf>)> {
    if let Some(logs_dir) = path.parent().filter(|p| p.file_name().is_some_and(|n| n == "logs")) {
        if let Some(sys_dir) = logs_dir.parent().filter(|p| p.file_name().is_some_and(|n| n == ".system_generated")) {
            if let Some(conv_dir) = sys_dir.parent() {
                if let Some(cid) = conv_dir.file_name().and_then(|n| n.to_str()) {
                    if let Some(brain_dir) = conv_dir.parent().filter(|p| p.file_name().is_some_and(|n| n == "brain")) {
                        if let Some(root) = brain_dir.parent() {
                            return Some((cid.to_string(), Some(root.to_path_buf())));
                        }
                    }
                }
            }
        }
    }
    let cid = path.file_stem()?.to_str()?.to_string();
    Some((cid, None))
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                out.push((hi << 4) | lo);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn decode_file_uri(uri: &str) -> Option<String> {
    let trimmed = uri.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Some(rest) = trimmed.strip_prefix("file://") {
        let decoded = percent_decode(rest);
        if !decoded.is_empty() {
            return Some(decoded);
        }
    } else if trimmed.starts_with('/') {
        return Some(trimmed.to_string());
    }
    None
}

pub fn cwd_from_workspace_uris(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(list) = serde_json::from_str::<Vec<String>>(trimmed) {
        for item in list {
            if let Some(cwd) = decode_file_uri(&item) {
                return Some(cwd);
            }
        }
    }
    let stripped = trimmed
        .trim_start_matches('[')
        .trim_end_matches(']')
        .split(',')
        .next()
        .unwrap_or("")
        .trim()
        .trim_matches('"')
        .trim_matches('\'');
    decode_file_uri(stripped)
}

pub fn read_summary_meta(root: &Path, cid: &str) -> SummaryMeta {
    let db = root.join("conversation_summaries.db");
    if !db.is_file() {
        return SummaryMeta::default();
    }
    let Ok(conn) = Connection::open_with_flags(
        &db,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) else {
        return SummaryMeta::default();
    };
    let _ = conn.busy_timeout(Duration::from_millis(500));

    let mut meta = SummaryMeta::default();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT workspace_uris, parent_conversation_id, nesting_depth \
         FROM conversation_summaries WHERE conversation_id = ?1 LIMIT 1",
    ) {
        if let Ok(Some((uris, parent_cid, depth))) = stmt
            .query_row([cid], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<i64>>(2)?,
                ))
            })
            .map(Some)
        {
            meta.cwd = uris.as_deref().and_then(cwd_from_workspace_uris);
            let has_parent = parent_cid.as_deref().is_some_and(|p| !p.trim().is_empty());
            let has_depth = depth.unwrap_or(0) > 0;
            meta.is_subagent = has_parent || has_depth;
        }
    } else if let Ok(mut stmt) = conn.prepare(
        "SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?1 LIMIT 1",
    ) {
        if let Ok(uris) = stmt.query_row([cid], |row| row.get::<_, Option<String>>(0)) {
            meta.cwd = uris.as_deref().and_then(cwd_from_workspace_uris);
        }
    }

    if !meta.is_subagent {
        if let Ok(mut stmt) =
            conn.prepare("SELECT parent_id FROM conversation_parents WHERE conversation_id = ?1 LIMIT 1")
        {
            if let Ok(parent_id) = stmt.query_row([cid], |row| row.get::<_, Option<String>>(0)) {
                if parent_id.as_deref().is_some_and(|p| !p.trim().is_empty()) {
                    meta.is_subagent = true;
                }
            }
        }
    }
    meta
}

fn unwrap_user_request(content: &str) -> &str {
    if let Some(start) = content.find("<USER_REQUEST>") {
        let after = &content[start + "<USER_REQUEST>".len()..];
        if let Some(end) = after.find("</USER_REQUEST>") {
            return after[..end].trim();
        }
    }
    let mut end = content.len();
    for marker in ["<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>"] {
        if let Some(pos) = content.find(marker) {
            end = end.min(pos);
        }
    }
    content[..end].trim()
}

pub fn model_name_from_user_input(content: &str) -> Option<String> {
    let open = "<USER_SETTINGS_CHANGE>";
    let pos = content.rfind(open)?;
    let after_open = &content[pos + open.len()..];
    let block = match after_open.find("</USER_SETTINGS_CHANGE>") {
        Some(close) => &after_open[..close],
        None => after_open,
    };
    let setting_pos = block.find("`Model Selection`")?;
    let after_setting = &block[setting_pos + "`Model Selection`".len()..];
    let from_pos = after_setting.find("from")?;
    let after_from = &after_setting[from_pos + "from".len()..];
    let to_pos = after_from.find(" to ")?;
    let raw = after_from[to_pos + " to ".len()..].trim_start();
    let mut cut = raw.len();
    for (idx, ch) in raw.char_indices() {
        if ch == '\n' || ch == '\r' || ch == '<' {
            cut = idx;
            break;
        }
        if ch == '.' {
            let next = raw[idx + 1..].chars().next();
            if next.is_none() || next.is_some_and(|c| c.is_whitespace() || c == '<') {
                cut = idx;
                break;
            }
        }
    }
    let model = raw[..cut].trim().trim_end_matches('.').trim();
    if model.is_empty() {
        None
    } else {
        Some(model.to_string())
    }
}

fn int_of(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        _ => None,
    }
}

fn is_tool_row(kind: &str, source: Option<&str>) -> bool {
    matches!(
        kind,
        "RUN_COMMAND"
            | "VIEW_FILE"
            | "LIST_DIRECTORY"
            | "CODE_ACTION"
            | "SEARCH_WEB"
            | "GREP_SEARCH"
            | "GENERIC"
    ) || (source == Some("MODEL")
        && !matches!(kind, "PLANNER_RESPONSE" | "CHECKPOINT" | "SYSTEM_MESSAGE"))
}

pub fn read(path: &Path, window: &Window) -> Option<SessionStats> {
    let (cid, root) = conv_id_and_root(path)?;
    let bytes = std::fs::read(path).ok()?;
    let mut b = Builder::new(Runtime::Antigravity, cid.clone());
    b.examined = bytes.len() as u64;

    let meta = root.as_deref().map(|r| read_summary_meta(r, &cid)).unwrap_or_default();
    b.cwd = meta.cwd;

    let mut rows: Vec<ParsedRow> = Vec::new();
    for (line_idx, line) in jsonl::lines(&bytes, 0).enumerate() {
        let Some(entry) = jsonl::parse(line) else { continue };
        let Some(obj) = entry.as_object() else { continue };
        if !obj.contains_key("step_index") && !obj.contains_key("type") {
            continue;
        }
        let kind = obj.get("type").and_then(Value::as_str).unwrap_or("").to_string();
        let source = obj.get("source").and_then(Value::as_str).map(str::to_string);
        let step_index = obj.get("step_index").and_then(Value::as_i64);
        let ts = jsonl::parse_ts(obj.get("created_at"));
        let (user_prompt, model_selection) = if kind == "USER_INPUT" {
            let content = obj.get("content").and_then(Value::as_str).unwrap_or("");
            let unwrapped = unwrap_user_request(content);
            let prompt = !unwrapped.is_empty() && unwrapped != "None";
            let model = model_name_from_user_input(content);
            (prompt, model)
        } else {
            (false, None)
        };
        rows.push(ParsedRow {
            step_index,
            line_idx,
            kind,
            source,
            ts,
            user_prompt,
            model_selection,
            input_tokens: int_of(obj.get("input_tokens")),
            cache_read_tokens: int_of(obj.get("cache_read_tokens")),
            output_tokens: int_of(obj.get("output_tokens")),
        });
    }

    rows.sort_by(|a, c| {
        a.step_index
            .unwrap_or(i64::MAX)
            .cmp(&c.step_index.unwrap_or(i64::MAX))
            .then(a.line_idx.cmp(&c.line_idx))
    });

    let mut has_prompt = false;
    let mut current_model: Option<String> = None;
    for row in rows {
        if row.kind == "USER_INPUT" {
            if let Some(m) = row.model_selection {
                current_model = Some(m);
            }
            if row.user_prompt {
                has_prompt = true;
            }
            if let Some(t) = row.ts {
                b.activity.push(t);
            }
        } else if row.kind == "PLANNER_RESPONSE" || is_tool_row(&row.kind, row.source.as_deref()) {
            if let Some(t) = row.ts {
                b.activity.push(t);
            }
        }

        if (row.kind == "PLANNER_RESPONSE" || row.kind == "CHECKPOINT")
            && (row.input_tokens.is_some()
                || row.cache_read_tokens.is_some()
                || row.output_tokens.is_some())
        {
            let id = row
                .step_index
                .map(|s| format!("step:{s}"))
                .unwrap_or_else(|| format!("line:{}", row.line_idx));
            b.observations.push(Observation {
                record: String::new(),
                scope: Scope::Message,
                counter: Counter::Absolute,
                response_id: None,
                turn_id: None,
                message_id: Some(id.clone()),
                scope_id: Some(id),
                counter_name: None,
                model: None,
                context_model: current_model.clone(),
                normalized: Normalized::inclusive(
                    row.input_tokens,
                    row.cache_read_tokens,
                    None,
                    row.output_tokens,
                    None,
                    None,
                    None,
                ),
                ts: row.ts,
            });
        }
    }

    b.excluded = if meta.is_subagent {
        Some("subagent")
    } else if let Some(reason) = cwd_exclusion(b.cwd.as_deref()) {
        Some(reason)
    } else if !has_prompt {
        Some("empty_history")
    } else {
        None
    };

    Some(b.finish(window))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(name: &str) -> Self {
            let p = std::env::temp_dir().join(format!(
                "sesskit-antigravity-test-{name}-{}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::create_dir_all(&p).unwrap();
            Self(p)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn write_summaries_db(
        root: &Path,
        rows: &[(&str, &str, &str, i64)],
        parents: &[(&str, &str)],
    ) {
        let conn = Connection::open(root.join("conversation_summaries.db")).unwrap();
        conn.execute_batch(
            "CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                workspace_uris TEXT NOT NULL DEFAULT '',
                parent_conversation_id TEXT NOT NULL DEFAULT '',
                nesting_depth INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE conversation_parents (
                conversation_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL
            );",
        )
        .unwrap();
        for (cid, uris, parent, depth) in rows {
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?1, ?2, ?3, ?4)",
                rusqlite::params![cid, uris, parent, depth],
            )
            .unwrap();
        }
        for (cid, pid) in parents {
            conn.execute(
                "INSERT INTO conversation_parents VALUES (?1, ?2)",
                rusqlite::params![cid, pid],
            )
            .unwrap();
        }
    }

    fn write_transcript(root: &Path, cid: &str, name: &str, lines: &[&str]) -> PathBuf {
        let dir = root.join("brain").join(cid).join(".system_generated").join("logs");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        std::fs::write(&path, format!("{}\n", lines.join("\n"))).unwrap();
        path
    }

    #[test]
    fn tokens_sum_uncached_plus_cache_read_and_track_context_model() {
        let tmp = TempDir::new("tokens");
        let work = tmp.0.join("proj");
        std::fs::create_dir_all(&work).unwrap();
        let root = tmp.0.join(".gemini/antigravity-cli");
        std::fs::create_dir_all(&root).unwrap();
        let uri = serde_json::to_string(&vec![format!("file://{}", work.display())]).unwrap();
        write_summaries_db(&root, &[("conv-1", &uri, "", 0)], &[]);

        let l0 = r#"{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"2026-11-10T10:00:00Z","content":"<USER_REQUEST>hello</USER_REQUEST><USER_SETTINGS_CHANGE>The user changed setting `Model Selection` from None to Gemini 4 Carbon (High). No need to comment.</USER_SETTINGS_CHANGE>"}"#;
        let l1 = r#"{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"2026-11-10T10:00:05Z","input_tokens":22438,"cache_read_tokens":0,"output_tokens":612,"content":"hi"}"#;
        let l2 = r#"{"step_index":2,"source":"MODEL","type":"GENERIC","status":"DONE","created_at":"2026-11-10T10:00:10Z","content":"ok"}"#;
        let l3 = r#"{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"2026-11-10T10:00:15Z","input_tokens":2946,"cache_read_tokens":22439,"output_tokens":345,"content":"done"}"#;
        let l4 = r#"{"step_index":4,"source":"SYSTEM","type":"CHECKPOINT","status":"DONE","created_at":"2026-11-10T10:00:20Z","input_tokens":100,"cache_read_tokens":200,"output_tokens":50,"content":"compact"}"#;
        let l5 = r#"{"step_index":5,"source":"SYSTEM","type":"SYSTEM_MESSAGE","status":"DONE","created_at":"2026-11-10T10:00:25Z","content":"ignored"}"#;
        let tpath = write_transcript(&root, "conv-1", "transcript_full.jsonl", &[l0, l1, l2, l3, l4, l5]);

        let window = Window {
            start: 1_700_000_000.0,
            end: 2_000_000_000.0,
            full: false,
        };
        let s = read(&tpath, &window).unwrap();
        assert_eq!(s.runtime, Runtime::Antigravity);
        assert_eq!(s.runtime.id(), "antigravity");
        assert!(s.excluded.is_none());
        // USER_INPUT (1) + PLANNER_RESPONSE (2) + GENERIC (1) = 4 activity timestamps
        // CHECKPOINT & SYSTEM_MESSAGE do not add to activity.
        assert_eq!(s.activity.len(), 4);
        // Step 1: 22438+0+612 = 23050; Step 3: 2946+22439+345 = 25730; Step 4 (CHECKPOINT): 100+200+50 = 350 -> 49130
        assert_eq!(s.window_tokens, 49_130);
        assert_eq!(s.known_total, Some(49_130));
        assert_eq!(
            s.window_models,
            vec![(Some("Gemini 4 Carbon (High)".to_string()), 49_130)]
        );
    }

    #[test]
    fn subagents_are_excluded_across_all_signals() {
        let tmp = TempDir::new("subagents");
        let work = tmp.0.join("proj");
        std::fs::create_dir_all(&work).unwrap();
        let root = tmp.0.join(".gemini/antigravity");
        std::fs::create_dir_all(&root).unwrap();
        let uri = serde_json::to_string(&vec![format!("file://{}", work.display())]).unwrap();
        write_summaries_db(
            &root,
            &[
                ("top", &uri, "", 0),
                ("sub-parent-col", &uri, "top", 0),
                ("sub-depth", &uri, "", 1),
                ("sub-table", &uri, "", 0),
            ],
            &[("sub-table", "top")],
        );
        let row = r#"{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"2026-11-10T10:00:00Z","content":"<USER_REQUEST>hi</USER_REQUEST>"}"#;
        for cid in ["top", "sub-parent-col", "sub-depth", "sub-table"] {
            write_transcript(&root, cid, "transcript_full.jsonl", &[row]);
        }
        let window = Window {
            start: 0.0,
            end: 2_000_000_000.0,
            full: true,
        };
        let collected = collect(&tmp.0, &window);
        let by_id: HashMap<&str, &SessionStats> =
            collected.iter().map(|s| (s.id.as_str(), s)).collect();
        assert_eq!(by_id["top"].excluded, None);
        assert_eq!(by_id["sub-parent-col"].excluded, Some("subagent"));
        assert_eq!(by_id["sub-depth"].excluded, Some("subagent"));
        assert_eq!(by_id["sub-table"].excluded, Some("subagent"));
    }

    #[test]
    fn full_transcript_wins_over_slim_sibling() {
        let tmp = TempDir::new("full_pref");
        let root = tmp.0.join(".config/antigravity");
        std::fs::create_dir_all(&root).unwrap();
        let line = r#"{"step_index":0,"type":"USER_INPUT","created_at":"2026-11-10T10:00:00Z","content":"hi"}"#;
        write_transcript(&root, "cid-1", "transcript.jsonl", &[line]);
        let full = write_transcript(&root, "cid-1", "transcript_full.jsonl", &[line]);
        let found = discover(&tmp.0, 0.0);
        assert_eq!(found, vec![full]);
    }

    #[test]
    fn model_selection_handles_version_dots_and_trailing_period() {
        assert_eq!(
            model_name_from_user_input(
                "prefix <USER_SETTINGS_CHANGE>The user changed setting `Model Selection` from None to Gemini 2.5 Pro. No need to comment.</USER_SETTINGS_CHANGE>"
            ).as_deref(),
            Some("Gemini 2.5 Pro")
        );
        assert_eq!(
            model_name_from_user_input(
                "<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` from Auto to Claude 4.5 Sonnet</USER_SETTINGS_CHANGE>"
            ).as_deref(),
            Some("Claude 4.5 Sonnet")
        );
    }
}
