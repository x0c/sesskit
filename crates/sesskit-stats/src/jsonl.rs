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

/// A top-level JSON value located without decoding it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Raw<'a> {
    /// String contents between the quotes (escapes left as written).
    Str(&'a [u8]),
    /// Object or array text including its brackets.
    Obj(&'a [u8]),
    Arr(&'a [u8]),
    /// `true`, `false`, `null` or a number.
    Lit(&'a [u8]),
}

impl<'a> Raw<'a> {
    pub fn str(self) -> Option<&'a str> {
        match self {
            Raw::Str(s) => std::str::from_utf8(s).ok(),
            _ => None,
        }
    }

    pub fn is_true(self) -> bool {
        // Python truthiness for the flags used here (bool or non-empty string).
        match self {
            Raw::Lit(l) => l == b"true" || (l != b"false" && l != b"null" && l != b"0"),
            Raw::Str(s) => !s.is_empty(),
            Raw::Obj(o) | Raw::Arr(o) => o.len() > 2,
        }
    }
}

/// End (exclusive) of the string starting at `i` (on its opening quote);
/// jumps between quotes and backslashes with SIMD search.
fn string_end(line: &[u8], mut i: usize) -> Option<usize> {
    i += 1;
    while i < line.len() {
        let at = i + memchr::memchr2(b'"', b'\\', &line[i..])?;
        if line[at] == b'"' {
            return Some(at + 1);
        }
        i = at + 2;
    }
    None
}

/// Values of the requested top-level keys of one JSON object (first wins),
/// skipping every other value without decoding it.
pub fn top_level<'a, const N: usize>(line: &'a [u8], keys: [&[u8]; N]) -> [Option<Raw<'a>>; N] {
    let mut out = [None; N];
    let n = line.len();
    let ws = |i: &mut usize| {
        while *i < n && line[*i].is_ascii_whitespace() {
            *i += 1;
        }
    };
    let mut i = 0;
    ws(&mut i);
    if i >= n || line[i] != b'{' {
        return out;
    }
    i += 1;
    loop {
        ws(&mut i);
        if i >= n || line[i] != b'"' {
            return out;
        }
        let Some(kend) = string_end(line, i) else { return out };
        let key = &line[i + 1..kend - 1];
        i = kend;
        ws(&mut i);
        if i >= n || line[i] != b':' {
            return out;
        }
        i += 1;
        ws(&mut i);
        if i >= n {
            return out;
        }
        let start = i;
        let value = match line[i] {
            b'"' => {
                let Some(e) = string_end(line, i) else { return out };
                i = e;
                Raw::Str(&line[start + 1..e - 1])
            }
            b'{' | b'[' => {
                let mut depth = 0i32;
                while i < n {
                    // Skip plain bytes up to the next quote or bracket.
                    if !matches!(line[i], b'"' | b'{' | b'[' | b'}' | b']') {
                        match line[i..].iter().position(|b| matches!(b, b'"' | b'{' | b'[' | b'}' | b']')) {
                            Some(off) => i += off,
                            None => break,
                        }
                    }
                    match line[i] {
                        b'"' => {
                            let Some(e) = string_end(line, i) else { return out };
                            i = e;
                            continue;
                        }
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
                if line[start] == b'{' {
                    Raw::Obj(&line[start..i])
                } else {
                    Raw::Arr(&line[start..i])
                }
            }
            _ => {
                while i < n && line[i] != b',' && line[i] != b'}' && !line[i].is_ascii_whitespace() {
                    i += 1;
                }
                Raw::Lit(&line[start..i])
            }
        };
        if let Some(k) = keys.iter().position(|k| *k == key) {
            if out[k].is_none() {
                out[k] = Some(value);
                if out.iter().all(Option::is_some) {
                    return out;
                }
            }
        }
        ws(&mut i);
        if i < n && line[i] == b',' {
            i += 1;
        } else {
            return out;
        }
    }
}

/// RFC 3339 timestamp from a raw string value.
pub fn raw_ts(raw: Option<Raw>) -> Option<f64> {
    let text = raw?.str()?.trim();
    OffsetDateTime::parse(text, &Rfc3339).ok().map(|t| t.unix_timestamp_nanos() as f64 / 1e9)
}

pub fn contains(line: &[u8], needle: &[u8]) -> bool {
    memchr::memmem::find(line, needle).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn top_level_skips_nested_values() {
        let line = br#"{"type":"x","details":{"id":"nested","a":[{"id":"deep"}],"s":"q\"}"},"id":"top","n":12,"flag":true,"parentId":null}"#;
        let [t, id, n, flag, parent, missing] = top_level(line, [b"type", b"id", b"n", b"flag", b"parentId", b"nope"]);
        assert_eq!(t, Some(Raw::Str(b"x")));
        assert_eq!(id, Some(Raw::Str(b"top")));
        assert_eq!(n, Some(Raw::Lit(b"12")));
        assert!(flag.unwrap().is_true());
        assert_eq!(parent, Some(Raw::Lit(b"null")));
        assert_eq!(missing, None);
    }
}
