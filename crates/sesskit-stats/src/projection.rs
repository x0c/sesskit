//! Compact JSON projections for Python listing paths, not runtime interpretation.
//!
//! Serde's `IgnoredAny` discards tool payloads without allocating their object
//! trees. Selected leaves retain raw JSON (including duplicate keys and number
//! spelling); Python remains responsible for decoding and interpreting them.

use std::fmt;

use serde::de::{DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::Deserializer;
use serde_json::value::RawValue;

type Fields = &'static [(&'static str, Selection)];

#[derive(Clone, Copy)]
enum Selection {
    Raw,
    Fields(Fields),
}

use Selection::{Fields as Nested, Raw};

const TEXT: Fields = &[("type", Raw), ("text", Raw)];
const CONTENT: Selection = Nested(TEXT);
const CLAUDE: Fields = &[
    ("type", Raw),
    ("isMeta", Raw),
    ("isSidechain", Raw),
    ("message", Nested(&[("content", CONTENT)])),
    (
        "attachment",
        Nested(&[
            ("type", Raw),
            ("prompt", Raw),
            ("humanTurn", Raw),
            ("origin", Nested(&[("kind", Raw)])),
        ]),
    ),
];
const CODEX: Fields = &[
    ("type", Raw),
    ("timestamp", Raw),
    (
        "payload",
        Nested(&[
            ("type", Raw),
            ("role", Raw),
            ("message", Raw),
            ("timestamp", Raw),
            ("content", CONTENT),
        ]),
    ),
];
const OPENCODE: Fields = &[
    ("text", Raw),
    ("content", CONTENT),
    ("error", Raw),
    ("finish", Raw),
    ("outcome", Raw),
    ("description", Raw),
    ("metadata", Nested(&[("source", Raw)])),
];

fn fields(profile: &str) -> Option<Fields> {
    match profile {
        "claude_excerpt" => Some(CLAUDE),
        "codex_excerpt" => Some(CODEX),
        "opencode_list" => Some(OPENCODE),
        _ => None,
    }
}

struct Project(Fields);

impl<'de> DeserializeSeed<'de> for Project {
    type Value = String;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<String, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Project {
    type Value = String;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON projection container")
    }

    fn visit_str<E: serde::de::Error>(self, value: &str) -> Result<String, E> {
        serde_json::to_string(value).map_err(E::custom)
    }

    fn visit_unit<E: serde::de::Error>(self) -> Result<String, E> {
        Ok("null".to_owned())
    }

    fn visit_bool<E: serde::de::Error>(self, value: bool) -> Result<String, E> {
        Ok(value.to_string())
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<String, M::Error> {
        let mut out = String::from("{");
        while let Some(key) = map.next_key::<String>()? {
            if let Some((_, selection)) = self.0.iter().find(|(name, _)| *name == key) {
                let value = match selection {
                    Raw => map.next_value::<&RawValue>()?.get().to_owned(),
                    Nested(fields) => map.next_value_seed(Project(fields))?,
                };
                if out.len() > 1 {
                    out.push(',');
                }
                // A decoded key can contain quotes or escapes; never concatenate
                // it into JSON without encoding it again.
                out.push_str(&serde_json::to_string(&key).map_err(serde::de::Error::custom)?);
                out.push(':');
                out.push_str(&value);
            } else {
                map.next_value::<IgnoredAny>()?;
            }
        }
        out.push('}');
        Ok(out)
    }

    fn visit_seq<S: SeqAccess<'de>>(self, mut seq: S) -> Result<String, S::Error> {
        let mut out = String::from("[");
        while let Some(value) = seq.next_element_seed(Project(self.0))? {
            if out.len() > 1 {
                out.push(',');
            }
            out.push_str(&value);
        }
        out.push(']');
        Ok(out)
    }
}

fn project_value(raw: &str, fields: Fields) -> Result<String, serde_json::Error> {
    let mut deserializer = serde_json::Deserializer::from_str(raw);
    // Shape mismatches use the original decoder, never an empty success.
    let result = deserializer.deserialize_map(Project(fields))?;
    deserializer.end()?;
    Ok(result)
}

/// None means unsupported/malformed input: the caller must use its original
/// decoder, never interpret this as an empty or successfully parsed record.
pub fn project_json(raw: &str, profile: &str) -> Option<String> {
    project_value(raw, fields(profile)?).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn discards_tool_payloads_but_preserves_text_and_error() {
        let input = json!({"content": [
            {"type": "text", "text": "reply"},
            {"type": "tool", "state": {"input": "x".repeat(1_000_000)}}
        ], "finish": "stop", "error": {"status": 429, "message": "quota"}})
        .to_string();
        let result = project_json(&input, "opencode_list").unwrap();
        assert!(result.len() < 200);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&result).unwrap(),
            json!({
                "content": [{"type": "text", "text": "reply"}, {"type": "tool"}],
                "finish": "stop", "error": {"status": 429, "message": "quota"}
            })
        );
    }

    #[test]
    fn raw_leaves_keep_duplicate_keys_large_numbers_and_escapes() {
        let input = r#"{"te\u0078t":"first","text":"last","error":{"n":99999999999999999999999999999999},"finish":null}"#;
        assert_eq!(
            project_json(input, "opencode_list").unwrap(),
            r#"{"text":"first","text":"last","error":{"n":99999999999999999999999999999999},"finish":null}"#
        );
    }

    #[test]
    fn malformed_and_unknown_profiles_are_not_empty_success() {
        for input in ["{", "{\"text\":\"ok\",\"other\":[}", "{} trailing"] {
            assert!(project_json(input, "opencode_list").is_none());
        }
        assert!(project_json("{}", "unknown").is_none());
        assert!(project_json("null", "opencode_list").is_none());
    }

    #[test]
    fn claude_human_evidence_and_content_shapes_survive() {
        let input = r#"{"type":"attachment","isMeta":false,"attachment":{"type":"queued_command","prompt":"hi","humanTurn":true,"origin":{"kind":"human","extra":1}},"message":{"content":[{"type":"text","text":"plain"}]}}"#;
        let result = project_json(input, "claude_excerpt").unwrap();
        assert!(result.contains("\"humanTurn\":true"));
        assert!(result.contains("\"kind\":\"human\""));
        assert!(result.contains("\"text\":\"plain\""));
        assert!(!result.contains("extra"));
    }
}
