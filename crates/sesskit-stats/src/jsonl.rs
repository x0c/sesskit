//! JSONL helpers: complete-line iteration, timestamps and window pruning.

use serde_json::Value;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

/// Complete lines only: a trailing record without `\n` belongs to a writer
/// still appending and is held back, as in `native_jsonl.read_jsonl_snapshot`.
pub fn lines(bytes: &[u8], from: usize) -> impl Iterator<Item = &[u8]> {
    let end = match memchr::memrchr(b'\n', bytes) {
        Some(i) if i + 1 >= from => i + 1,
        _ => from,
    };
    let body = &bytes[from.min(end)..end];
    body.split(|b| *b == b'\n').filter(|l| !l.is_empty())
}

pub fn parse(line: &[u8]) -> Option<Value> {
    match serde_json::from_slice::<Value>(line) {
        Ok(v @ Value::Object(_)) => Some(v),
        _ => None,
    }
}

/// ISO-8601 / RFC 3339 text to epoch seconds (`parse_timestamp`).
pub fn parse_ts(value: Option<&Value>) -> Option<f64> {
    let text = value?.as_str()?.trim();
    if text.is_empty() {
        return None;
    }
    let parsed = OffsetDateTime::parse(text, &Rfc3339).ok().or_else(|| {
        // Python's fromisoformat also accepts a space separator.
        let fixed = text.replacen(' ', "T", 1);
        OffsetDateTime::parse(&fixed, &Rfc3339).ok()
    })?;
    Some(parsed.unix_timestamp_nanos() as f64 / 1e9)
}

/// Cheap timestamp probe for window pruning: the first `"timestamp":"..."`
/// value on a line, without decoding the whole record.
pub fn probe_ts(line: &[u8]) -> Option<f64> {
    const KEY: &[u8] = b"\"timestamp\":\"";
    let at = memchr::memmem::find(line, KEY)? + KEY.len();
    let rest = &line[at..];
    let close = memchr::memchr(b'"', rest)?;
    let text = std::str::from_utf8(&rest[..close]).ok()?;
    OffsetDateTime::parse(text, &Rfc3339).ok().map(|t| t.unix_timestamp_nanos() as f64 / 1e9)
}

/// Byte offset of the first line whose probed timestamp is at or after
/// `start`, assuming append-ordered history. Binary search over line starts;
/// lines without a timestamp are skipped forward. Returns 0 when the head is
/// already inside the window or no timestamp is found.
pub fn window_offset(bytes: &[u8], start: f64) -> usize {
    let line_at = |pos: usize| -> Option<(usize, usize, Option<f64>)> {
        // Start of the line containing/after `pos`.
        let begin = if pos == 0 {
            0
        } else {
            memchr::memchr(b'\n', &bytes[pos..]).map(|i| pos + i + 1)?
        };
        if begin >= bytes.len() {
            return None;
        }
        let mut cur = begin;
        // Walk forward until a line with a timestamp (bounded).
        for _ in 0..64 {
            let stop = memchr::memchr(b'\n', &bytes[cur..]).map(|i| cur + i).unwrap_or(bytes.len());
            if let Some(ts) = probe_ts(&bytes[cur..stop]) {
                return Some((begin, cur, Some(ts)));
            }
            if stop >= bytes.len() {
                break;
            }
            cur = stop + 1;
        }
        Some((begin, cur, None))
    };
    match line_at(0) {
        Some((_, _, Some(ts))) if ts >= start => return 0,
        None => return 0,
        _ => {}
    }
    let (mut lo, mut hi) = (0usize, bytes.len());
    // Invariant: the line at `lo` is before the window; answer is in (lo, hi].
    while hi > lo + 4096 {
        let mid = lo + (hi - lo) / 2;
        match line_at(mid) {
            // Only a timestamped line strictly inside (lo, hi) narrows the span.
            Some((_, at, Some(ts))) if at > lo && at < hi => {
                if ts >= start {
                    hi = at;
                } else {
                    lo = at;
                }
            }
            _ => hi = mid,
        }
    }
    // Linear finish inside the last small span; back up to a line start.
    let begin = memchr::memrchr(b'\n', &bytes[..lo]).map(|i| i + 1).unwrap_or(0);
    let mut cur = begin;
    while cur < bytes.len() {
        let stop = memchr::memchr(b'\n', &bytes[cur..]).map(|i| cur + i).unwrap_or(bytes.len());
        if let Some(ts) = probe_ts(&bytes[cur..stop]) {
            if ts >= start {
                return cur;
            }
        }
        cur = stop + 1;
    }
    bytes.len()
}
