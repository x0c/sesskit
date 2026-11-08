//! Shared time aggregates over the projection's activity timestamps.
//!
//! Rules (docs/NATIVE_STATS_ENGINE_DESIGN.md, "Shared time aggregates"):
//! consumers call these instead of re-implementing them, so every card and
//! report agrees on active time and concurrency.

use time::{OffsetDateTime, UtcOffset};

/// Consecutive activity at most this many seconds apart is one active interval.
pub const ACTIVE_GAP: f64 = 300.0;
/// Seconds an interval extends past its last record, so a lone record still
/// occupies time and simultaneous replies overlap.
pub const EVENT_TAIL: f64 = 60.0;

/// Merge one session's ascending activity timestamps into active intervals.
pub fn active_intervals(times: &[f64]) -> Vec<(f64, f64)> {
    let mut out: Vec<(f64, f64)> = Vec::new();
    for &t in times {
        match out.last_mut() {
            Some(last) if t - last.1 <= ACTIVE_GAP => last.1 = t,
            _ => out.push((t, t)),
        }
    }
    out.into_iter().map(|(a, b)| (a, b + EVENT_TAIL)).collect()
}

/// Maximum number of intervals open at one instant and the first instant it
/// is reached. Intervals that only touch are not concurrent.
pub fn peak_concurrency(intervals: &[(f64, f64)]) -> (usize, Option<f64>) {
    let mut points: Vec<(f64, i32)> = Vec::with_capacity(intervals.len() * 2);
    for &(a, b) in intervals {
        points.push((a, 1));
        points.push((b, -1));
    }
    // Ends sort before starts at the same instant: touching is not concurrent.
    points.sort_by(|x, y| x.0.total_cmp(&y.0).then(x.1.cmp(&y.1)));
    let (mut cur, mut peak, mut at) = (0i32, 0i32, None);
    for (t, d) in points {
        cur += d;
        if cur > peak {
            peak = cur;
            at = Some(t);
        }
    }
    (peak.max(0) as usize, at)
}

/// Add interval seconds to a `[weekday Mon=0][hour]` histogram, split at
/// local hour boundaries for `offset`.
pub fn add_weekday_hours(histogram: &mut [[f64; 24]; 7], intervals: &[(f64, f64)], offset: UtcOffset) {
    for &(start, end) in intervals {
        let mut cur = start;
        while cur < end {
            let Ok(dt) = OffsetDateTime::from_unix_timestamp(cur.floor() as i64) else { break };
            let local = dt.to_offset(offset);
            let into_hour = (local.minute() as f64) * 60.0 + local.second() as f64 + cur.fract();
            let stop = (cur - into_hour + 3600.0).min(end);
            let day = local.weekday().number_days_from_monday() as usize;
            histogram[day][local.hour() as usize] += stop - cur;
            if stop <= cur {
                break;
            }
            cur = stop;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_gap_splits_intervals_and_touching_is_not_concurrent() {
        let a = active_intervals(&[0.0, 86400.0]);
        let b = active_intervals(&[43200.0, 43260.0]);
        assert_eq!(a.len(), 2);
        assert_eq!(peak_concurrency(&[a, b].concat()).0, 1);
        assert_eq!(peak_concurrency(&[(0.0, 100.0), (100.0, 200.0)]).0, 1);
        assert_eq!(peak_concurrency(&[(0.0, 100.0), (50.0, 150.0), (60.0, 70.0)]), (3, Some(60.0)));
    }

    #[test]
    fn gap_boundary_and_tail() {
        assert_eq!(active_intervals(&[0.0, 300.0]), vec![(0.0, 360.0)]);
        assert_eq!(active_intervals(&[0.0, 300.5]), vec![(0.0, 60.0), (300.5, 360.5)]);
        assert_eq!(active_intervals(&[10.0]), vec![(10.0, 70.0)]);
        assert!(active_intervals(&[]).is_empty());
        assert_eq!(peak_concurrency(&[]), (0, None));
    }

    #[test]
    fn hours_split_at_boundaries() {
        let mut h = [[0.0; 24]; 7];
        // 2026-11-02 12:50:00 UTC is a Monday.
        let t = 1_793_623_800.0;
        add_weekday_hours(&mut h, &[(t, t + 1200.0)], UtcOffset::UTC);
        assert!((h[0][12] - 600.0).abs() < 1e-6);
        assert!((h[0][13] - 600.0).abs() < 1e-6);
        // Same instant at UTC+8 lands on Monday 20:50.
        let mut h8 = [[0.0; 24]; 7];
        add_weekday_hours(&mut h8, &[(t, t + 600.0)], UtcOffset::from_hms(8, 0, 0).unwrap());
        assert!((h8[0][20] - 600.0).abs() < 1e-6);
    }
}
