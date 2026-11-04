//! Per-session projection shared by every runtime reader.

use serde::Serialize;

use crate::usage::{self, Contribution, Observation};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Runtime {
    Claude,
    Codex,
    Opencode,
    Pi,
}

impl Runtime {
    pub fn id(self) -> &'static str {
        match self {
            Runtime::Claude => "claude",
            Runtime::Codex => "codex",
            Runtime::Opencode => "opencode",
            Runtime::Pi => "pi",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Window {
    pub start: f64,
    pub end: f64,
    /// Read whole histories (parity mode) instead of pruning to the window.
    pub full: bool,
}

impl Window {
    pub fn contains(&self, ts: f64) -> bool {
        ts >= self.start && ts <= self.end
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct SessionStats {
    pub runtime: Runtime,
    pub id: String,
    pub cwd: Option<String>,
    /// Why the session is outside the listed set (subagent, missing cwd...).
    pub excluded: Option<&'static str>,
    /// In-window activity timestamps, ascending.
    pub activity: Vec<f64>,
    /// In-window token total attributed by native record time.
    pub window_tokens: i64,
    /// Tokens whose record carries no usable time (not attributed to any window).
    pub untimed_tokens: i64,
    /// `known_total` over every record read (the whole session in full mode).
    pub known_total: Option<i64>,
    /// Bytes or rows of history examined.
    pub examined: u64,
}

pub struct Builder {
    pub runtime: Runtime,
    pub id: String,
    pub cwd: Option<String>,
    pub excluded: Option<&'static str>,
    pub activity: Vec<f64>,
    pub observations: Vec<Observation>,
    pub examined: u64,
}

impl Builder {
    pub fn new(runtime: Runtime, id: String) -> Self {
        Builder { runtime, id, cwd: None, excluded: None, activity: Vec::new(), observations: Vec::new(), examined: 0 }
    }

    pub fn finish(mut self, window: &Window) -> SessionStats {
        let records = usage::reconcile(&self.observations);
        let refs: Vec<&usage::Record> = records.iter().collect();
        let (known, parts) = usage::known_total(&refs);
        let (mut window_tokens, mut untimed) = (0i64, 0i64);
        for Contribution { tokens, ts } in parts {
            match ts {
                Some(t) if window.contains(t) => window_tokens += tokens,
                Some(_) => {}
                None => untimed += tokens,
            }
        }
        self.activity.retain(|t| window.contains(*t));
        self.activity.sort_by(f64::total_cmp);
        SessionStats {
            runtime: self.runtime,
            id: self.id,
            cwd: self.cwd,
            excluded: self.excluded,
            activity: self.activity,
            window_tokens,
            untimed_tokens: untimed,
            known_total: known,
            examined: self.examined,
        }
    }
}

/// SessKit's default listing drop: missing or deleted working directory, or a
/// `.sesskit-ignore` marker in the cwd or an ancestor.
pub fn cwd_exclusion(cwd: Option<&str>) -> Option<&'static str> {
    let Some(cwd) = cwd.filter(|c| !c.is_empty()) else {
        return Some("missing_cwd");
    };
    if std::env::var("SESSKIT_INCLUDE_EPHEMERAL").as_deref() == Ok("1") {
        return None;
    }
    let path = std::path::Path::new(cwd);
    if !path.is_dir() {
        return Some("missing_cwd");
    }
    let mut cur = Some(path);
    while let Some(dir) = cur {
        if dir.join(".sesskit-ignore").exists() {
            return Some("ephemeral_marker");
        }
        cur = dir.parent();
    }
    None
}
