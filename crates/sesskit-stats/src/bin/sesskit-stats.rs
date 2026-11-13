//! `sesskit-stats`: windowed activity/usage projection as JSON.
//!
//! Usage: sesskit-stats [--days N | --since EPOCH] [--until EPOCH] [--full] [--sessions]

use std::time::{Instant, SystemTime};

use sesskit_stats::{collect, home_dir, Window};

fn local_midnight_days_back(now: f64, days: i64) -> f64 {
    let offset = time::UtcOffset::current_local_offset().unwrap_or(time::UtcOffset::UTC);
    let now_dt = time::OffsetDateTime::from_unix_timestamp(now as i64).unwrap().to_offset(offset);
    let midnight = now_dt.replace_time(time::Time::MIDNIGHT) - time::Duration::days(days - 1);
    midnight.unix_timestamp() as f64
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let get = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs_f64();
    let days: i64 = get("--days").and_then(|d| d.parse().ok()).unwrap_or(7);
    let start = get("--since").and_then(|s| s.parse().ok()).unwrap_or_else(|| local_midnight_days_back(now, days));
    let end = get("--until").and_then(|s| s.parse().ok()).unwrap_or(now);
    let window = Window { start, end, full: args.iter().any(|a| a == "--full") };
    let Some(home) = get("--home").map(Into::into).or_else(home_dir) else {
        eprintln!("sesskit-stats: cannot determine the home directory");
        std::process::exit(1);
    };
    let t0 = Instant::now();
    let sessions = collect(&home, &window);
    let elapsed = t0.elapsed().as_secs_f64();
    let listed: Vec<_> = sessions.iter().filter(|s| s.excluded.is_none() && !s.activity.is_empty()).collect();
    let tokens: i64 = listed.iter().map(|s| s.window_tokens).sum();
    let examined: u64 = sessions.iter().map(|s| s.examined).sum();
    let mut out = serde_json::json!({
        "window": {"start": start, "end": end, "full": window.full},
        "elapsed_s": elapsed,
        "sessions_read": sessions.len(),
        "sessions": listed.len(),
        "tokens": tokens,
        "examined_bytes": examined,
        "threads": rayon::current_num_threads(),
    });
    if args.iter().any(|a| a == "--timing") {
        let mut per: std::collections::BTreeMap<&str, (usize, u64)> = Default::default();
        for s in &sessions {
            let e = per.entry(s.runtime.id()).or_default();
            e.0 += 1;
            e.1 += s.examined;
        }
        out["per_runtime"] = serde_json::to_value(&per).unwrap();
        let t = Instant::now();
        let src = sesskit_stats::discover(&home, &window);
        out["discover_s"] = serde_json::json!(t.elapsed().as_secs_f64());
        out["files"] = serde_json::json!({
            "claude": src.claude.len(),
            "codex": src.codex.len(),
            "pi": src.pi.len(),
            "antigravity": src.antigravity.len(),
        });
        for (name, files, f) in [
            ("claude", &src.claude, sesskit_stats::claude::read as fn(&std::path::Path, &Window) -> Option<sesskit_stats::SessionStats>),
            ("codex", &src.codex, sesskit_stats::codex::read),
            ("pi", &src.pi, sesskit_stats::pi::read),
            ("antigravity", &src.antigravity, sesskit_stats::antigravity::read),
        ] {
            let t = Instant::now();
            let mut slowest = (0.0f64, String::new(), 0u64);
            for p in files.iter() {
                let ft = Instant::now();
                let _ = f(p, &window);
                let d = ft.elapsed().as_secs_f64();
                if d > slowest.0 {
                    slowest = (d, p.display().to_string(), std::fs::metadata(p).map(|m| m.len()).unwrap_or(0));
                }
            }
            out[format!("serial_{name}_s")] = serde_json::json!(t.elapsed().as_secs_f64());
            out[format!("slowest_{name}")] = serde_json::json!([slowest.0, slowest.2, slowest.1]);
        }
        let t = Instant::now();
        for db in &src.opencode {
            let _ = sesskit_stats::opencode::read(db, &window);
        }
        out["opencode_s"] = serde_json::json!(t.elapsed().as_secs_f64());
    }
    if args.iter().any(|a| a == "--sessions") {
        out["detail"] = serde_json::to_value(&sessions).unwrap();
    }
    println!("{}", out);
}
