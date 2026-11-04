//! SessKit native stats engine: windowed activity and usage projection.
//!
//! Design: `docs/NATIVE_STATS_ENGINE_DESIGN.md`. Interpretation rules mirror
//! the Python reference in `src/sesskit/usage.py` for the total-token metric.

pub mod claude;
pub mod codex;
pub mod jsonl;
pub mod opencode;
pub mod pi;
pub mod session;
pub mod usage;

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use rayon::prelude::*;

pub use session::{Runtime, SessionStats, Window};

/// User home directory (`HOME`, else `USERPROFILE` on Windows).
pub fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var_os("USERPROFILE").filter(|v| !v.is_empty()))
        .map(PathBuf::from)
}

fn mtime(path: &Path) -> Option<f64> {
    let m = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(m.duration_since(SystemTime::UNIX_EPOCH).ok()?.as_secs_f64())
}

fn jsonl_files(dir: &Path, depth: usize, start: f64, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(kind) = entry.file_type() else { continue };
        if kind.is_dir() {
            if depth > 0 {
                jsonl_files(&path, depth - 1, start, out);
            }
        } else if path.extension().is_some_and(|e| e == "jsonl") && mtime(&path).is_some_and(|m| m >= start) {
            out.push(path);
        }
    }
}

#[derive(Default)]
pub struct Sources {
    pub claude: Vec<PathBuf>,
    pub codex: Vec<PathBuf>,
    pub pi: Vec<PathBuf>,
    pub opencode: Vec<PathBuf>,
}

/// History files modified inside the window (directory listing + mtime only).
pub fn discover(home: &Path, window: &Window) -> Sources {
    let mut s = Sources::default();
    // Claude: top-level session files only (subagent transcripts live deeper).
    jsonl_files(&home.join(".claude").join("projects"), 1, window.start, &mut s.claude);
    s.claude.retain(|p| p.parent().and_then(Path::parent) == Some(&home.join(".claude").join("projects")));
    jsonl_files(&home.join(".codex").join("sessions"), 4, window.start, &mut s.codex);
    jsonl_files(&home.join(".pi").join("agent").join("sessions"), 2, window.start, &mut s.pi);
    s.opencode = opencode::db_paths(home);
    s
}

/// Read every discovered session in parallel.
pub fn collect(home: &Path, window: &Window) -> Vec<SessionStats> {
    let sources = discover(home, window);
    let jobs: Vec<(Runtime, PathBuf)> = sources
        .claude
        .into_iter()
        .map(|p| (Runtime::Claude, p))
        .chain(sources.codex.into_iter().map(|p| (Runtime::Codex, p)))
        .chain(sources.pi.into_iter().map(|p| (Runtime::Pi, p)))
        .collect();
    let (mut files, dbs): (Vec<SessionStats>, Vec<SessionStats>) = rayon::join(
        || {
            jobs.par_iter()
                .filter_map(|(rt, path)| match rt {
                    Runtime::Claude => claude::read(path, window),
                    Runtime::Codex => codex::read(path, window),
                    Runtime::Pi => pi::read(path, window),
                    Runtime::Opencode => None,
                })
                .collect()
        },
        || sources.opencode.iter().flat_map(|db| opencode::read(db, window)).collect(),
    );
    files.extend(dbs);
    files
}
