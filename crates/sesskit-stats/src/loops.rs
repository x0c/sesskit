//! Fast cross-runtime `loop_count` calculation with Python parity.
//!
//! One loop = one assistant model-decision iteration, merging parallel tool
//! calls and fragmented streaming rows of the same response into 1, and
//! excluding system, metadata, compaction, and synthetic/empty error rows.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::time::Duration;

use rayon::prelude::*;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde::de::{DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};

use crate::jsonl::{self, Raw};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FallbackError {
    NonStandardJson,
}

pub type CountResult = Result<Option<u64>, FallbackError>;

#[derive(Default)]
struct MaybeString(Option<String>);

impl<'de> Deserialize<'de> for MaybeString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = MaybeString;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(MaybeString(Some(v.to_owned())))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(MaybeString(Some(v)))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(MaybeString(None))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(MaybeString(None))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(MaybeString(None))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(MaybeString(None))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(MaybeString(None))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(MaybeString(None))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(MaybeString(None))
            }
        }
        deserializer.deserialize_any(V)
    }
}

#[derive(Default)]
struct NonEmptyStrFlag(bool);

impl<'de> Deserialize<'de> for NonEmptyStrFlag {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = NonEmptyStrFlag;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(!v.trim().is_empty()))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(NonEmptyStrFlag(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(NonEmptyStrFlag(false))
            }
        }
        deserializer.deserialize_any(V)
    }
}

#[derive(Default)]
struct Truthy(bool);

impl<'de> Deserialize<'de> for Truthy {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Truthy;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(Truthy(v))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(Truthy(v != 0))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(Truthy(v != 0))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
                Ok(Truthy(v != 0.0))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(Truthy(!v.is_empty()))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(Truthy(false))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut any = false;
                while seq.next_element::<IgnoredAny>()?.is_some() {
                    any = true;
                }
                Ok(Truthy(any))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut any = false;
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {
                    any = true;
                }
                Ok(Truthy(any))
            }
        }
        deserializer.deserialize_any(V)
    }
}

#[derive(Default)]
struct ExactTrue(bool);

impl<'de> Deserialize<'de> for ExactTrue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = ExactTrue;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(ExactTrue(v))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(ExactTrue(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(ExactTrue(false))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(ExactTrue(false))
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> {
                Ok(ExactTrue(false))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(ExactTrue(false))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(ExactTrue(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(ExactTrue(false))
            }
        }
        deserializer.deserialize_any(V)
    }
}

struct MaybeMap<T>(Option<T>);

impl<T> Default for MaybeMap<T> {
    fn default() -> Self {
        Self(None)
    }
}

impl<'de, T: Deserialize<'de>> Deserialize<'de> for MaybeMap<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V<T>(std::marker::PhantomData<T>);
        impl<'de, T: Deserialize<'de>> Visitor<'de> for V<T> {
            type Value = MaybeMap<T>;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Self::Value, A::Error> {
                let inner = T::deserialize(serde::de::value::MapAccessDeserializer::new(map))?;
                Ok(MaybeMap(Some(inner)))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(MaybeMap(None))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(MaybeMap(None))
            }
        }
        deserializer.deserialize_any(V(std::marker::PhantomData))
    }
}

#[derive(Default)]
struct NonEmptyTextOrList(bool);

impl<'de> Deserialize<'de> for NonEmptyTextOrList {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = NonEmptyTextOrList;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("string or array")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(!v.trim().is_empty()))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut any = false;
                while seq.next_element::<IgnoredAny>()?.is_some() {
                    any = true;
                }
                Ok(NonEmptyTextOrList(any))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(false))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(false))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(false))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(NonEmptyTextOrList(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(NonEmptyTextOrList(false))
            }
        }
        deserializer.deserialize_any(V)
    }
}

const LINE_READER_BUF_BYTES: usize = 64 * 1024;
const LINE_BUF_SHRINK_THRESHOLD: usize = 256 * 1024;

fn trim_cr(line: &[u8]) -> &[u8] {
    line.strip_suffix(b"\r").unwrap_or(line)
}

fn for_each_line_bytes<F>(bytes: &[u8], mut visit: F) -> Result<(), FallbackError>
where
    F: FnMut(&[u8]) -> Result<bool, FallbackError>,
{
    for raw_line in bytes.split(|b| *b == b'\n') {
        if !visit(trim_cr(raw_line))? {
            break;
        }
    }
    Ok(())
}

fn for_each_line_reader<R: BufRead, F>(mut reader: R, mut visit: F) -> Result<(), FallbackError>
where
    F: FnMut(&[u8]) -> Result<bool, FallbackError>,
{
    let mut buf = Vec::with_capacity(LINE_READER_BUF_BYTES);
    loop {
        buf.clear();
        let n = match reader.read_until(b'\n', &mut buf) {
            Ok(n) => n,
            Err(_) => break,
        };
        if n == 0 {
            break;
        }
        let raw = buf.strip_suffix(b"\n").unwrap_or(&buf);
        let keep_going = visit(trim_cr(raw))?;
        if buf.capacity() > LINE_BUF_SHRINK_THRESHOLD {
            buf = Vec::with_capacity(LINE_READER_BUF_BYTES);
        }
        if !keep_going {
            break;
        }
    }
    Ok(())
}

fn open_line_reader(path: &Path) -> Option<BufReader<File>> {
    File::open(path)
        .ok()
        .map(|file| BufReader::with_capacity(LINE_READER_BUF_BYTES, file))
}

fn looks_like_nonstandard_json(raw: &[u8]) -> bool {
    jsonl::contains(raw, b"NaN")
        || jsonl::contains(raw, b"Infinity")
        || has_lone_surrogate_escape(raw)
}

fn has_lone_surrogate_escape(raw: &[u8]) -> bool {
    let mut i = 0;
    while let Some(pos) = memchr::memmem::find(&raw[i..], b"\\u") {
        let start = i + pos + 2;
        if start + 4 <= raw.len() {
            let hex = &raw[start..start + 4];
            if matches!(hex[0], b'd' | b'D')
                && matches!(hex[1], b'8' | b'9' | b'a'..=b'f' | b'A'..=b'F')
            {
                return true;
            }
        }
        i = start;
    }
    false
}

fn parse_object<'de, T: Deserialize<'de>>(raw: &'de [u8]) -> Result<Option<T>, FallbackError> {
    let trimmed = raw.trim_ascii();
    if !trimmed.starts_with(b"{") {
        return Ok(None);
    }
    match serde_json::from_slice::<T>(trimmed) {
        Ok(val) => Ok(Some(val)),
        Err(_) if looks_like_nonstandard_json(trimmed) => Err(FallbackError::NonStandardJson),
        Err(_) => Ok(None),
    }
}

// --- Claude ----------------------------------------------------------------

#[derive(Deserialize)]
struct ClaudeRow {
    #[serde(rename = "type", default)]
    kind: MaybeString,
    #[serde(rename = "isMeta", default)]
    is_meta: Truthy,
    #[serde(rename = "isSidechain", default)]
    is_sidechain: Truthy,
    #[serde(rename = "isApiErrorMessage", default)]
    is_api_error: ExactTrue,
    #[serde(default)]
    message: MaybeMap<ClaudeMessage>,
}

#[derive(Deserialize)]
struct ClaudeMessage {
    #[serde(default)]
    id: MaybeString,
    #[serde(default)]
    model: MaybeString,
    #[serde(default)]
    content: NonEmptyTextOrList,
}

fn claude_with_lines<F>(iter: F) -> CountResult
where
    F: FnOnce(&mut dyn FnMut(&[u8]) -> Result<bool, FallbackError>) -> Result<(), FallbackError>,
{
    let mut count = 0u64;
    let mut seen_ids: HashSet<String> = HashSet::new();
    let mut visit = |line: &[u8]| -> Result<bool, FallbackError> {
        if line.trim_ascii().is_empty() {
            return Ok(true);
        }
        if !jsonl::contains(line, b"\\u") {
            let ty = jsonl::top_level(line, [b"type"])[0].and_then(Raw::str);
            if ty != Some("assistant") {
                return Ok(true);
            }
        }
        let Some(row) = parse_object::<ClaudeRow>(line)? else {
            return Ok(true);
        };
        if row.is_meta.0 || row.is_sidechain.0 || row.is_api_error.0 {
            return Ok(true);
        }
        if row.kind.0.as_deref() != Some("assistant") {
            return Ok(true);
        }
        let Some(msg) = row.message.0 else {
            return Ok(true);
        };
        if msg.model.0.as_deref().map(str::trim) == Some("<synthetic>") {
            return Ok(true);
        }
        if !msg.content.0 {
            return Ok(true);
        }
        if let Some(id) = msg.id.0.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            if seen_ids.insert(id.to_owned()) {
                count += 1;
            }
        } else {
            count += 1;
        }
        Ok(true)
    };
    iter(&mut visit)?;
    Ok(Some(count))
}

pub fn claude_from_bytes(bytes: &[u8]) -> CountResult {
    claude_with_lines(|visit| for_each_line_bytes(bytes, visit))
}

pub fn claude_from_reader<R: BufRead>(reader: R) -> CountResult {
    claude_with_lines(|visit| for_each_line_reader(reader, visit))
}

// --- Codex -----------------------------------------------------------------

#[derive(Deserialize)]
struct CodexRow {
    #[serde(rename = "type", default)]
    kind: MaybeString,
    #[serde(default)]
    payload: MaybeMap<CodexPayload>,
}

#[derive(Deserialize)]
struct CodexPayload {
    #[serde(rename = "type", default)]
    ptype: MaybeString,
    #[serde(default)]
    role: MaybeString,
    #[serde(default)]
    message: NonEmptyStrFlag,
    #[serde(default)]
    last_agent_message: NonEmptyStrFlag,
}

fn codex_with_lines<F>(iter: F) -> CountResult
where
    F: FnOnce(&mut dyn FnMut(&[u8]) -> Result<bool, FallbackError>) -> Result<(), FallbackError>,
{
    let mut ri_loops = 0u64;
    let mut in_step = false;
    let mut legacy_loops = 0u64;
    let mut turn_has_agent_msg = false;

    let mut visit = |line: &[u8]| -> Result<bool, FallbackError> {
        if line.trim_ascii().is_empty() {
            return Ok(true);
        }
        if !jsonl::contains(line, b"\\u") {
            let ty = jsonl::top_level(line, [b"type"])[0].and_then(Raw::str);
            if !matches!(
                ty,
                Some("response_item" | "token_usage_record" | "event_msg" | "compacted")
            ) {
                return Ok(true);
            }
        }
        let Some(row) = parse_object::<CodexRow>(line)? else {
            return Ok(true);
        };
        let Some(kind) = row.kind.0.as_deref() else {
            return Ok(true);
        };
        match kind {
            "token_usage_record" | "compacted" => {
                in_step = false;
            }
            "response_item" => {
                let Some(pl) = row.payload.0 else {
                    return Ok(true);
                };
                let pt = pl.ptype.0.as_deref();
                let role = pl.role.0.as_deref();
                if matches!(
                    pt,
                    Some(
                        "reasoning"
                            | "function_call"
                            | "custom_tool_call"
                            | "web_search_call"
                            | "local_shell_call"
                    )
                ) || (pt == Some("message") && role == Some("assistant"))
                {
                    if !in_step {
                        ri_loops += 1;
                        in_step = true;
                    }
                } else if matches!(
                    pt,
                    Some("function_call_output" | "custom_tool_call_output")
                ) || (pt == Some("message") && role == Some("user"))
                {
                    in_step = false;
                }
            }
            "event_msg" => {
                let Some(pl) = row.payload.0 else {
                    return Ok(true);
                };
                let pt = pl.ptype.0.as_deref();
                if matches!(
                    pt,
                    Some(
                        "token_count"
                            | "user_message"
                            | "task_started"
                            | "task_complete"
                            | "turn_aborted"
                    )
                ) {
                    in_step = false;
                }
                match pt {
                    Some("user_message" | "task_started" | "turn_aborted") => {
                        turn_has_agent_msg = false;
                    }
                    Some("agent_message") => {
                        if pl.message.0 {
                            legacy_loops += 1;
                            turn_has_agent_msg = true;
                        }
                    }
                    Some("task_complete") => {
                        if !turn_has_agent_msg && pl.last_agent_message.0 {
                            legacy_loops += 1;
                        }
                        turn_has_agent_msg = false;
                    }
                    _ => {}
                }
            }
            _ => {}
        }
        Ok(true)
    };

    iter(&mut visit)?;
    Ok(Some(if ri_loops > 0 { ri_loops } else { legacy_loops }))
}

pub fn codex_from_bytes(bytes: &[u8]) -> CountResult {
    codex_with_lines(|visit| for_each_line_bytes(bytes, visit))
}

pub fn codex_from_reader<R: BufRead>(reader: R) -> CountResult {
    codex_with_lines(|visit| for_each_line_reader(reader, visit))
}

// --- Pi --------------------------------------------------------------------

#[derive(Default)]
struct PiContentOutput(bool);

impl<'de> Deserialize<'de> for PiContentOutput {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct PartSeed;
        impl<'de> DeserializeSeed<'de> for PartSeed {
            type Value = bool;
            fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<bool, D::Error> {
                struct PartVisitor;
                impl<'de> Visitor<'de> for PartVisitor {
                    type Value = bool;
                    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                        f.write_str("pi content part")
                    }
                    fn visit_map<A: MapAccess<'de>>(
                        self,
                        mut map: A,
                    ) -> Result<bool, A::Error> {
                        let mut ptype: Option<String> = None;
                        let mut has_text = false;
                        let mut has_thinking = false;
                        while let Some(key) = map.next_key::<String>()? {
                            match key.as_str() {
                                "type" => ptype = map.next_value::<MaybeString>()?.0,
                                "text" => has_text = map.next_value::<NonEmptyStrFlag>()?.0,
                                "thinking" => has_thinking = map.next_value::<NonEmptyStrFlag>()?.0,
                                _ => {
                                    map.next_value::<IgnoredAny>()?;
                                }
                            }
                        }
                        let Some(t) = ptype.as_deref().map(str::trim).filter(|s| !s.is_empty())
                        else {
                            return Ok(false);
                        };
                        Ok(match t {
                            "text" | "thinking" => has_text || has_thinking,
                            _ => true,
                        })
                    }
                    fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_unit<E: serde::de::Error>(self) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_seq<A: SeqAccess<'de>>(
                        self,
                        mut seq: A,
                    ) -> Result<bool, A::Error> {
                        while seq.next_element::<IgnoredAny>()?.is_some() {}
                        Ok(false)
                    }
                }
                deserializer.deserialize_any(PartVisitor)
            }
        }

        struct ContentVisitor;
        impl<'de> Visitor<'de> for ContentVisitor {
            type Value = PiContentOutput;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("string or array")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(PiContentOutput(!v.trim().is_empty()))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> Result<Self::Value, A::Error> {
                let mut valid = false;
                while let Some(part_ok) = seq.next_element_seed(PartSeed)? {
                    valid |= part_ok;
                }
                Ok(PiContentOutput(valid))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(PiContentOutput(false))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(PiContentOutput(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(Self::Value::default())
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(Self::Value::default())
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(Self::Value::default())
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(Self::Value::default())
            }
        }
        deserializer.deserialize_any(ContentVisitor)
    }
}

#[derive(Deserialize)]
struct PiRow {
    #[serde(rename = "type", default)]
    kind: MaybeString,
    #[serde(default)]
    id: MaybeString,
    #[serde(rename = "parentId", default)]
    parent_id: MaybeString,
    #[serde(default)]
    message: MaybeMap<PiMessage>,
}

#[derive(Deserialize)]
struct PiMessage {
    #[serde(default)]
    role: MaybeString,
    #[serde(default)]
    content: PiContentOutput,
}

struct PiEntry {
    is_message: bool,
    id: Option<String>,
    parent_id: Option<String>,
    is_valid_assistant: bool,
}

fn pi_with_lines<F>(iter: F) -> CountResult
where
    F: FnOnce(&mut dyn FnMut(&[u8]) -> Result<bool, FallbackError>) -> Result<(), FallbackError>,
{
    let mut header_checked = false;
    let mut valid_header = false;
    let mut entries: Vec<PiEntry> = Vec::new();

    let mut visit = |line: &[u8]| -> Result<bool, FallbackError> {
        if line.trim_ascii().is_empty() {
            return Ok(true);
        }
        let Some(row) = parse_object::<PiRow>(line)? else {
            return Ok(true);
        };
        if !header_checked {
            header_checked = true;
            if row.kind.0.as_deref() != Some("session")
                || row.id.0.as_deref().map_or(true, str::is_empty)
            {
                return Ok(false);
            }
            valid_header = true;
            entries.push(PiEntry {
                is_message: false,
                id: row.id.0,
                parent_id: row.parent_id.0,
                is_valid_assistant: false,
            });
            return Ok(true);
        }
        let is_message = row.kind.0.as_deref() == Some("message") && row.message.0.is_some();
        let is_valid_assistant = row.message.0.as_ref().is_some_and(|m| {
            is_message && m.role.0.as_deref() == Some("assistant") && m.content.0
        });
        entries.push(PiEntry {
            is_message,
            id: row.id.0,
            parent_id: row.parent_id.0,
            is_valid_assistant,
        });
        Ok(true)
    };

    iter(&mut visit)?;

    if !valid_header {
        return Ok(None);
    }

    let messages: Vec<&PiEntry> = entries.iter().filter(|e| e.is_message).collect();
    if messages.is_empty() {
        return Ok(Some(0));
    }
    if messages.iter().any(|m| m.id.is_none()) {
        return Ok(Some(
            messages.iter().filter(|m| m.is_valid_assistant).count() as u64,
        ));
    }

    let mut by_id: HashMap<&str, usize> = HashMap::new();
    for (idx, item) in entries.iter().enumerate() {
        if let Some(id) = item.id.as_deref() {
            by_id.insert(id, idx);
        }
    }

    let mut leaf_idx: Option<usize> = entries
        .last()
        .and_then(|e| e.id.as_deref())
        .and_then(|id| by_id.get(id).copied());
    if leaf_idx.is_none() {
        for item in entries.iter().rev() {
            if let Some(id) = item.id.as_deref() {
                if let Some(&idx) = by_id.get(id) {
                    leaf_idx = Some(idx);
                    break;
                }
            }
        }
    }
    let Some(mut cur) = leaf_idx else {
        return Ok(Some(
            messages.iter().filter(|m| m.is_valid_assistant).count() as u64,
        ));
    };

    let mut visited: HashSet<usize> = HashSet::new();
    let mut count = 0u64;
    while visited.insert(cur) {
        let entry = &entries[cur];
        if entry.is_message && entry.is_valid_assistant {
            count += 1;
        }
        match entry
            .parent_id
            .as_deref()
            .and_then(|pid| by_id.get(pid).copied())
        {
            Some(next) => cur = next,
            None => break,
        }
    }
    Ok(Some(count))
}

pub fn pi_from_bytes(bytes: &[u8]) -> CountResult {
    pi_with_lines(|visit| for_each_line_bytes(bytes, visit))
}

pub fn pi_from_reader<R: BufRead>(reader: R) -> CountResult {
    pi_with_lines(|visit| for_each_line_reader(reader, visit))
}

// --- Antigravity -----------------------------------------------------------

#[derive(Default)]
struct Present(bool);

impl<'de> Deserialize<'de> for Present {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(IgnoredAny)?;
        Ok(Present(true))
    }
}

#[derive(Default)]
struct HasDictItem(bool);

impl<'de> Deserialize<'de> for HasDictItem {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ItemSeed;
        impl<'de> DeserializeSeed<'de> for ItemSeed {
            type Value = bool;
            fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<bool, D::Error> {
                struct V;
                impl<'de> Visitor<'de> for V {
                    type Value = bool;
                    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                        f.write_str("any")
                    }
                    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<bool, A::Error> {
                        while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                        Ok(true)
                    }
                    fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_unit<E: serde::de::Error>(self) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<bool, A::Error> {
                        while seq.next_element::<IgnoredAny>()?.is_some() {}
                        Ok(false)
                    }
                }
                deserializer.deserialize_any(V)
            }
        }
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = HasDictItem;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("array")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut found = false;
                while let Some(is_dict) = seq.next_element_seed(ItemSeed)? {
                    found |= is_dict;
                }
                Ok(HasDictItem(found))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(HasDictItem(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(HasDictItem(false))
            }
        }
        deserializer.deserialize_any(V)
    }
}

#[derive(Default)]
struct HasValidMedia(bool);

impl<'de> Deserialize<'de> for HasValidMedia {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ItemSeed;
        impl<'de> DeserializeSeed<'de> for ItemSeed {
            type Value = bool;
            fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<bool, D::Error> {
                struct V;
                impl<'de> Visitor<'de> for V {
                    type Value = bool;
                    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                        f.write_str("media item")
                    }
                    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<bool, A::Error> {
                        let mut ok = false;
                        while let Some(key) = map.next_key::<String>()? {
                            match key.as_str() {
                                "mime_type" | "uri" => {
                                    ok |= map.next_value::<NonEmptyStrFlag>()?.0;
                                }
                                _ => {
                                    map.next_value::<IgnoredAny>()?;
                                }
                            }
                        }
                        Ok(ok)
                    }
                    fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_unit<E: serde::de::Error>(self) -> Result<bool, E> {
                        Ok(false)
                    }
                    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<bool, A::Error> {
                        while seq.next_element::<IgnoredAny>()?.is_some() {}
                        Ok(false)
                    }
                }
                deserializer.deserialize_any(V)
            }
        }
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = HasValidMedia;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("media list")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut found = false;
                while let Some(item_ok) = seq.next_element_seed(ItemSeed)? {
                    found |= item_ok;
                }
                Ok(HasValidMedia(found))
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(HasValidMedia(false))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(HasValidMedia(false))
            }
        }
        deserializer.deserialize_any(V)
    }
}

#[derive(Deserialize)]
struct AgRow {
    #[serde(default)]
    step_index: Present,
    #[serde(rename = "type", default)]
    kind: MaybeString,
    #[serde(default)]
    content: MaybeString,
    #[serde(default)]
    thinking: MaybeString,
    #[serde(default)]
    tool_calls: HasDictItem,
    #[serde(default)]
    media: HasValidMedia,
}

fn antigravity_with_lines<F>(iter: F) -> CountResult
where
    F: FnOnce(&mut dyn FnMut(&[u8]) -> Result<bool, FallbackError>) -> Result<(), FallbackError>,
{
    let mut has_any_record = false;
    let mut count = 0u64;
    let mut visit = |line: &[u8]| -> Result<bool, FallbackError> {
        if line.trim_ascii().is_empty() {
            return Ok(true);
        }
        let Some(row) = parse_object::<AgRow>(line)? else {
            return Ok(true);
        };
        if !row.step_index.0 || row.kind.0.is_none() {
            return Ok(true);
        }
        has_any_record = true;
        if row.kind.0.as_deref() != Some("PLANNER_RESPONSE") {
            return Ok(true);
        }
        let has_content = row
            .content
            .0
            .as_deref()
            .map(str::trim)
            .is_some_and(|c| !c.is_empty() && c != "None");
        let has_thinking = row
            .thinking
            .0
            .as_deref()
            .is_some_and(|t| !t.trim().is_empty());
        if has_content || has_thinking || row.tool_calls.0 || row.media.0 {
            count += 1;
        }
        Ok(true)
    };
    iter(&mut visit)?;
    Ok(if has_any_record { Some(count) } else { None })
}

pub fn antigravity_from_bytes(bytes: &[u8]) -> CountResult {
    antigravity_with_lines(|visit| for_each_line_bytes(bytes, visit))
}

pub fn antigravity_from_reader<R: BufRead>(reader: R) -> CountResult {
    antigravity_with_lines(|visit| for_each_line_reader(reader, visit))
}

// --- Cursor ----------------------------------------------------------------

#[derive(Deserialize)]
struct CursorBlob {
    #[serde(default)]
    role: MaybeString,
    #[serde(default)]
    content: NonEmptyTextOrList,
}

pub fn cursor_from_store(path: &Path) -> CountResult {
    if !path.is_file() {
        return Ok(None);
    }
    let Ok(conn) = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) else {
        return Ok(None);
    };
    let _ = conn.busy_timeout(Duration::from_millis(500));
    let _ = conn.execute_batch("PRAGMA mmap_size = 4294967296;");
    let Ok(mut stmt) =
        conn.prepare("SELECT data FROM blobs WHERE substr(data, 1, 1) = X'7B'")
    else {
        return Ok(None);
    };
    let Ok(mut rows) = stmt.query([]) else {
        return Ok(None);
    };

    let mut has_json_blob = false;
    let mut loops = 0u64;
    loop {
        match rows.next() {
            Ok(Some(row)) => {
                let raw: &[u8] = match row.get_ref(0) {
                    Ok(ValueRef::Blob(b)) => b,
                    Ok(ValueRef::Text(t)) => t,
                    _ => continue,
                };
                if !raw.starts_with(b"{") {
                    continue;
                }
                let Some(blob) = parse_object::<CursorBlob>(raw)? else {
                    continue;
                };
                has_json_blob = true;
                if blob.role.0.as_deref() == Some("assistant") && blob.content.0 {
                    loops += 1;
                }
            }
            Ok(None) => break,
            Err(_) => return Ok(None),
        }
    }
    Ok(if has_json_blob { Some(loops) } else { None })
}

// --- Dispatch & batch ------------------------------------------------------

pub fn count_path(runtime: &str, path: &Path) -> CountResult {
    match runtime {
        "claude" => {
            let Some(reader) = open_line_reader(path) else {
                return Ok(None);
            };
            claude_from_reader(reader)
        }
        "codex" => {
            let Some(reader) = open_line_reader(path) else {
                return Ok(None);
            };
            codex_from_reader(reader)
        }
        "pi" => {
            let Some(reader) = open_line_reader(path) else {
                return Ok(None);
            };
            pi_from_reader(reader)
        }
        "antigravity" => {
            if path.extension().map_or(true, |e| e != "jsonl") {
                return Ok(None);
            }
            let Some(reader) = open_line_reader(path) else {
                return Ok(None);
            };
            antigravity_from_reader(reader)
        }
        "cursor" => cursor_from_store(path),
        _ => Ok(None),
    }
}

/// Parallel batch loop counter across `(runtime, path)` pairs.
/// Returns `(true, count)` when natively resolved, or `(false, None)` when a
/// file contains non-standard JSON and requires Python fallback.
pub fn count_many(items: &[(String, String)]) -> Vec<(bool, Option<u64>)> {
    items
        .par_iter()
        .map(|(runtime, path)| match count_path(runtime, Path::new(path)) {
            Ok(count) => (true, count),
            Err(FallbackError::NonStandardJson) => (false, None),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claude_merges_fragmented_message_ids_and_skips_synthetic() {
        let data = br#"{"type":"user","message":{"role":"user","content":"hi"}}
{"type":"assistant","message":{"id":"m1","model":"claude-sonnet","content":[{"type":"thinking","thinking":"..."}]}}
{"type":"assistant","message":{"id":"m1","model":"claude-sonnet","content":[{"type":"text","text":"step1"}]}}
{"type":"assistant","message":{"id":"m1","model":"claude-sonnet","content":[{"type":"tool_use","id":"c1","name":"Bash","input":{"cmd":"ls"}}]}}
{"type":"assistant","message":{"id":"m1","model":"claude-sonnet","content":[{"type":"tool_use","id":"c2","name":"Read","input":{"p":"a"}}]}}
{"type":"assistant","isMeta":true,"message":{"id":"meta1","content":[{"type":"text","text":"meta"}]}}
{"type":"assistant","isSidechain":true,"message":{"id":"side1","content":[{"type":"text","text":"side"}]}}
{"type":"assistant","isApiErrorMessage":true,"message":{"id":"err1","model":"<synthetic>","content":[{"type":"text","text":"401"}]}}
{"type":"assistant","message":{"id":"empty","content":[]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"legacy no id"}]}}
{"type":"assistant","message":{"id":"m2","model":"claude-sonnet","content":[{"type":"text","text":"done"}]}}"#;
        assert_eq!(claude_from_bytes(data), Ok(Some(3)));
    }

    #[test]
    fn codex_merges_parallel_calls_and_falls_back_to_legacy_event_msg() {
        let modern = br#"{"type":"session_meta","payload":{"cwd":"/tmp"}}
{"type":"event_msg","payload":{"type":"task_started","turn_id":"t1"}}
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"go"}]}}
{"type":"response_item","payload":{"type":"reasoning"}}
{"type":"event_msg","payload":{"type":"item_completed","turn_id":"t1"}}
{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"checking"}]}}
{"type":"response_item","payload":{"type":"custom_tool_call","call_id":"c1"}}
{"type":"response_item","payload":{"type":"custom_tool_call","call_id":"c2"}}
{"type":"token_usage_record","payload":{"turn_id":"t1"}}
{"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"c1"}}
{"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"c2"}}
{"type":"event_msg","payload":{"type":"token_count"}}
{"type":"token_usage_record","payload":{"turn_id":"t1","usage":{"output_tokens":0}}}
{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}}
{"type":"event_msg","payload":{"type":"task_complete","turn_id":"t1"}}"#;
        assert_eq!(codex_from_bytes(modern), Ok(Some(2)));

        let legacy = br#"{"type":"event_msg","payload":{"type":"user_message","message":"hi"}}
{"type":"event_msg","payload":{"type":"agent_message","message":"working"}}
{"type":"event_msg","payload":{"type":"task_complete","last_agent_message":null}}
{"type":"event_msg","payload":{"type":"user_message","message":"next"}}
{"type":"event_msg","payload":{"type":"task_complete","last_agent_message":"final"}}"#;
        assert_eq!(codex_from_bytes(legacy), Ok(Some(2)));
    }

    #[test]
    fn pi_counts_only_active_branch_and_skips_empty_error_stubs() {
        let data = br#"{"type":"session","id":"s1","cwd":"/tmp"}
{"type":"message","id":"u1","parentId":null,"message":{"role":"user","content":"hi"}}
{"type":"message","id":"old_a","parentId":"u1","message":{"role":"assistant","content":[{"type":"text","text":"old branch"}]}}
{"type":"message","id":"a1","parentId":"u1","message":{"role":"assistant","content":[{"type":"toolCall","id":"c1"},{"type":"toolCall","id":"c2"}]}}
{"type":"message","id":"tr1","parentId":"a1","message":{"role":"toolResult","toolCallId":"c1"}}
{"type":"message","id":"err","parentId":"tr1","message":{"role":"assistant","stopReason":"error","errorMessage":"ECONNRESET","content":[]}}
{"type":"message","id":"a2","parentId":"err","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"done"}]}}"#;
        assert_eq!(pi_from_bytes(data), Ok(Some(2)));
    }

    #[test]
    fn antigravity_counts_non_empty_planner_responses() {
        let data = br#"{"step_index":0,"type":"USER_INPUT","content":"hi"}
{"step_index":1,"type":"PLANNER_RESPONSE","content":"checking","tool_calls":[{"name":"a"},{"name":"b"}]}
{"step_index":2,"type":"GENERIC","content":"out1"}
{"step_index":3,"type":"GENERIC","content":"out2"}
{"step_index":4,"type":"CHECKPOINT","content":"compacted"}
{"step_index":5,"type":"ERROR_MESSAGE","error":"rate limit"}
{"step_index":6,"type":"PLANNER_RESPONSE","status":"CANCELED","content":"None"}
{"step_index":7,"type":"PLANNER_RESPONSE","status":"DONE","content":"final"}"#;
        assert_eq!(antigravity_from_bytes(data), Ok(Some(2)));
    }

    #[test]
    fn cursor_counts_assistant_blobs_and_returns_none_for_empty_store() {
        let dir = std::env::temp_dir().join(format!("sesskit-loops-cursor-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("store.db");
        {
            let conn = Connection::open(&db).unwrap();
            conn.execute_batch("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
                .unwrap();
            conn.execute(
                "INSERT INTO blobs VALUES ('0', X'010203')",
                [],
            )
            .unwrap();
            assert_eq!(cursor_from_store(&db), Ok(None));

            conn.execute(
                "INSERT INTO blobs VALUES ('1', ?)",
                [br#"{"role":"user","content":"hi"}"#.as_slice()],
            )
            .unwrap();
            assert_eq!(cursor_from_store(&db), Ok(Some(0)));

            conn.execute(
                "INSERT INTO blobs VALUES ('2', ?)",
                [br#"{"role":"assistant","content":[{"type":"reasoning","text":"hmm"},{"type":"tool-call","toolCallId":"1"},{"type":"tool-call","toolCallId":"2"}]}"#.as_slice()],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO blobs VALUES ('3', ?)",
                [br#"{"role":"tool","content":[{"type":"tool-result","toolCallId":"1"}]}"#.as_slice()],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO blobs VALUES ('4', ?)",
                [br#"{"role":"assistant","content":[]}"#.as_slice()],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO blobs VALUES ('5', ?)",
                [br#"{"role":"assistant","content":"done"}"#.as_slice()],
            )
            .unwrap();
        }
        assert_eq!(cursor_from_store(&db), Ok(Some(2)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn streaming_readers_match_bytes_across_large_lines_and_early_stop() {
        let padding = "x".repeat(300 * 1024);
        let codex = format!(
            "{{\"type\":\"session_meta\",\"payload\":{{\"cwd\":\"/tmp\"}}}}\r\n\
             {{\"type\":\"response_item\",\"payload\":{{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{{\"type\":\"output_text\",\"text\":\"{padding}\"}}]}}}}\r\n\
             {{\"type\":\"token_usage_record\",\"payload\":{{}}}}\n\
             {{\"type\":\"response_item\",\"payload\":{{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{{\"type\":\"output_text\",\"text\":\"done\"}}]}}}}"
        );
        assert_eq!(
            codex_from_reader(std::io::Cursor::new(codex.as_bytes())),
            codex_from_bytes(codex.as_bytes())
        );
        assert_eq!(codex_from_reader(std::io::Cursor::new(codex.as_bytes())), Ok(Some(2)));

        let invalid_pi = b"{\"type\":\"message\",\"id\":\"m1\",\"message\":{\"role\":\"assistant\",\"content\":\"hi\"}}\n";
        assert_eq!(pi_from_reader(std::io::Cursor::new(invalid_pi)), Ok(None));
        assert_eq!(pi_from_bytes(invalid_pi), Ok(None));

        let ag = b"{\"step_index\":0,\"type\":\"PLANNER_RESPONSE\",\"content\":\"ok\"}\r\n";
        assert_eq!(antigravity_from_reader(std::io::Cursor::new(ag)), Ok(Some(1)));
        let claude = b"{\"type\":\"assistant\",\"message\":{\"id\":\"m1\",\"content\":\"ok\"}}\n";
        assert_eq!(claude_from_reader(std::io::Cursor::new(claude)), Ok(Some(1)));
    }
}
