//! The shared projection parser and fingerprint.
//!
//! `share/broker_protocol.py:parse_projection_payload` is the ONE parser the
//! writer pre-check, the instance ACK and the auth loader share; a Rust consumer
//! that were more lenient would accept a projection the Python share refuses.
//! The fingerprint is SHA-256 over `json.dumps(projection, sort_keys=True)`.

use std::fmt;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::canonical::canonical_json;

pub const PROJECTION_SCHEMA_VERSIONS: [i64; 2] = [1, 2];
pub const PROJECTION_DEFAULT_MAX_CONCURRENCY: i64 = 1;
pub const PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY: i64 = 2;
pub const PROJECTION_MAX_CONCURRENCY_MIN: i64 = 1;
pub const PROJECTION_MAX_CONCURRENCY_MAX: i64 = 48;
pub const PROJECTION_LEGACY_MAX_CONCURRENCY_MAX: i64 = 64;
pub const PROJECTION_SHARE_MAX_CONCURRENCY_MIN: i64 = 1;
pub const PROJECTION_SHARE_MAX_CONCURRENCY_MAX: i64 = 64;
pub const PROJECTION_LEGACY_SHARE_MAX_CONCURRENCY_MAX: i64 = 128;

const TOP_LEVEL_KEYS: [&str; 4] = [
    "schema_version",
    "config_generation",
    "data",
    "share_max_concurrency",
];
const ENTRY_KEYS: [&str; 6] = [
    "key_id",
    "name",
    "secret_hash",
    "secret_version",
    "enabled",
    "max_concurrency",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectionError {
    pub code: &'static str,
    pub message: String,
}

impl ProjectionError {
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self {
            code: "bad_request",
            message: message.into(),
        }
    }
}

impl fmt::Display for ProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ProjectionError {}

/// Parse and normalize a projection exactly like the Python parser.
pub fn parse_projection_payload(payload: &Value) -> Result<Value, ProjectionError> {
    let object = payload
        .as_object()
        .ok_or_else(|| ProjectionError::bad_request("projection must be an object"))?;
    let schema = object.get("schema_version");
    let schema_value = schema
        .and_then(Value::as_i64)
        .filter(|value| PROJECTION_SCHEMA_VERSIONS.contains(value))
        .ok_or_else(|| ProjectionError::bad_request("unsupported projection schema"))?;
    let generation = object
        .get("config_generation")
        .and_then(Value::as_i64)
        .filter(|value| *value >= 0)
        .ok_or_else(|| ProjectionError::bad_request("invalid projection generation"))?;
    let entries = object
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| ProjectionError::bad_request("invalid projection data"))?;
    if object
        .keys()
        .any(|key| !TOP_LEVEL_KEYS.contains(&key.as_str()))
    {
        return Err(ProjectionError::bad_request(
            "projection has unknown fields",
        ));
    }
    let share_max = match object.get("share_max_concurrency") {
        None => PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY,
        Some(value) => parse_projection_limit(
            value,
            "share_max_concurrency",
            PROJECTION_SHARE_MAX_CONCURRENCY_MIN,
            PROJECTION_SHARE_MAX_CONCURRENCY_MAX,
            PROJECTION_LEGACY_SHARE_MAX_CONCURRENCY_MAX,
        )?,
    };
    let mut seen_ids = Vec::new();
    let mut seen_names = Vec::new();
    let mut normalized_entries = Vec::new();
    for item in entries {
        let entry = item
            .as_object()
            .ok_or_else(|| ProjectionError::bad_request("invalid projection entry"))?;
        let mut unknown: Vec<&str> = entry
            .keys()
            .map(String::as_str)
            .filter(|key| !ENTRY_KEYS.contains(key))
            .collect();
        if !unknown.is_empty() {
            unknown.sort_unstable();
            return Err(ProjectionError::bad_request(format!(
                "projection entry has unknown field(s): [{}]",
                unknown
                    .iter()
                    .map(|name| format!("'{name}'"))
                    .collect::<Vec<_>>()
                    .join(", ")
            )));
        }
        let key_id = entry
            .get("key_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ProjectionError::bad_request("invalid projection identity"))?;
        let name = entry
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ProjectionError::bad_request("invalid projection identity"))?;
        if seen_ids.iter().any(|seen| seen == key_id) || seen_names.iter().any(|seen| seen == name)
        {
            return Err(ProjectionError::bad_request(
                "duplicate projection identity",
            ));
        }
        seen_ids.push(key_id.to_string());
        seen_names.push(name.to_string());
        let digest = entry
            .get("secret_hash")
            .and_then(Value::as_str)
            .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
            .ok_or_else(|| ProjectionError::bad_request("invalid projection digest"))?;
        let version = entry
            .get("secret_version")
            .and_then(Value::as_i64)
            .filter(|value| *value >= 1)
            .ok_or_else(|| ProjectionError::bad_request("invalid secret version"))?;
        if entry.get("enabled").and_then(Value::as_bool).is_none() {
            return Err(ProjectionError::bad_request("invalid enabled state"));
        }
        let max_concurrency = match entry.get("max_concurrency") {
            None => PROJECTION_DEFAULT_MAX_CONCURRENCY,
            Some(value) => parse_projection_limit(
                value,
                "max_concurrency",
                PROJECTION_MAX_CONCURRENCY_MIN,
                PROJECTION_MAX_CONCURRENCY_MAX,
                PROJECTION_LEGACY_MAX_CONCURRENCY_MAX,
            )?,
        };
        let mut normalized = entry.clone();
        normalized.insert("max_concurrency".to_string(), Value::from(max_concurrency));
        normalized_entries.push(Value::Object(normalized));
        let _ = (digest, version);
    }
    Ok(json!({
        "schema_version": schema_value,
        "config_generation": generation,
        "share_max_concurrency": share_max,
        "data": normalized_entries,
    }))
}

fn parse_projection_limit(
    value: &Value,
    field: &str,
    minimum: i64,
    maximum: i64,
    legacy_maximum: i64,
) -> Result<i64, ProjectionError> {
    let number = value
        .as_i64()
        .filter(|value| (minimum..=legacy_maximum).contains(value))
        .ok_or_else(|| {
            ProjectionError::bad_request(format!(
                "projection field {field} must be an integer in {minimum}..{legacy_maximum}"
            ))
        })?;
    Ok(number.min(maximum))
}

/// SHA-256 over the canonical (sorted-key) projection JSON.
pub fn fingerprint(projection: &Value) -> String {
    let canonical = canonical_json(projection);
    let digest = Sha256::digest(canonical.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}
