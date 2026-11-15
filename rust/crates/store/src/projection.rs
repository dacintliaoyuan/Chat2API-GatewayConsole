//! AC3: the shared projection parser, fingerprint and atomic writer.
//!
//! `share/broker_protocol.py:parse_projection_payload` is the ONE parser the
//! writer pre-check, the instance ACK and the auth loader share; a Rust writer
//! that were more lenient would publish a projection the Python share refuses.
//! The fingerprint is SHA-256 over `json.dumps(projection, sort_keys=True)`, the
//! same call `share/management.py:_fingerprint` makes.

use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use um_contract::canonical::canonical_json;

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
    fn bad_request(message: impl Into<String>) -> Self {
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

/// The projection ACL boundary. A missing check is a refusal, never a pass.
pub trait ProjectionAcl: Send + Sync {
    fn check(&self, path: &Path) -> Result<(), String>;
}

/// An explicit test/deployment checker that accepts one fixed path.
pub struct AllowPath(pub PathBuf);

impl ProjectionAcl for AllowPath {
    fn check(&self, path: &Path) -> Result<(), String> {
        if path == self.0 {
            Ok(())
        } else {
            Err(format!(
                "projection ACL check failed: unexpected path {path:?}"
            ))
        }
    }
}

pub struct DenyAll;

impl ProjectionAcl for DenyAll {
    fn check(&self, _path: &Path) -> Result<(), String> {
        Err("projection ACL check failed: denied".to_string())
    }
}

/// POSIX permission checker, mirroring `PosixProjectionAclChecker`.
///
/// The parent directory must exist and must not be group/other writable; the
/// target must not be a symlink, must be a regular file, must be owned by the
/// expected uid and must not carry a group/other write bit. An absent target is
/// allowed (the writer is about to create it) but its parent is still checked.
pub struct PosixAcl {
    pub expected_uid: Option<u32>,
}

impl ProjectionAcl for PosixAcl {
    fn check(&self, path: &Path) -> Result<(), String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;

            let parent = path.parent().unwrap_or_else(|| Path::new("."));
            let parent_metadata = std::fs::metadata(parent)
                .map_err(|_| format!("projection parent unreadable: {}", parent.display()))?;
            if !parent_metadata.is_dir() {
                return Err(format!(
                    "projection parent is not a directory: {}",
                    parent.display()
                ));
            }
            if parent_metadata.mode() & 0o022 != 0 {
                return Err(format!(
                    "projection parent is group/other writable: {}",
                    parent.display()
                ));
            }
            let metadata = match std::fs::symlink_metadata(path) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
                Err(_) => return Err(format!("projection unreadable: {}", path.display())),
            };
            if metadata.file_type().is_symlink() {
                return Err(format!("projection is a symlink: {}", path.display()));
            }
            if !metadata.is_file() {
                return Err(format!(
                    "projection is not a regular file: {}",
                    path.display()
                ));
            }
            let expected_uid = self.expected_uid.unwrap_or_else(|| {
                // SAFETY: `geteuid` has no preconditions.
                unsafe { libc::geteuid() }
            });
            if metadata.uid() != expected_uid {
                return Err(format!(
                    "projection owner uid {} != {expected_uid}",
                    metadata.uid()
                ));
            }
            if metadata.mode() & 0o022 != 0 {
                return Err(format!(
                    "projection is group/other writable: {}",
                    path.display()
                ));
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Err("projection ACL check failed: no POSIX ACL on this platform".to_string())
        }
    }
}

/// Fixed-destination, fully validated, atomic projection writer.
pub struct ProtectedProjectionWriter<A: ProjectionAcl> {
    path: PathBuf,
    acl: A,
    last_written: Option<(i64, String)>,
}

impl<A: ProjectionAcl> ProtectedProjectionWriter<A> {
    /// Build the baseline FROM the protected landing spot.
    ///
    /// An existing but corrupt or invalid file refuses publication: it is never
    /// treated as an empty state.
    pub fn new(path: PathBuf, acl: A) -> Result<Self, ProjectionError> {
        let mut last_written = None;
        if path.exists() {
            let text = std::fs::read_to_string(&path).map_err(|_| {
                ProjectionError::bad_request(
                    "protected projection file is corrupt; refusing publication over it",
                )
            })?;
            let existing: Value = serde_json::from_str(&text).map_err(|_| {
                ProjectionError::bad_request(
                    "protected projection file is corrupt; refusing publication over it",
                )
            })?;
            let parsed = parse_projection_payload(&existing).map_err(|error| {
                ProjectionError::bad_request(format!(
                    "protected projection file is invalid; refusing publication over it: {error}"
                ))
            })?;
            let generation = parsed
                .get("config_generation")
                .and_then(Value::as_i64)
                .unwrap_or_default();
            last_written = Some((generation, fingerprint(&existing)));
        }
        Ok(Self {
            path,
            acl,
            last_written,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Validate, write a protected temp file, re-read it, check the ACL and
    /// only then atomically replace the projection.
    pub fn write(
        &mut self,
        projection: &Value,
        expected_generation: i64,
        expected_fingerprint: &str,
    ) -> Result<String, ProjectionError> {
        parse_projection_payload(projection)?;
        let generation = projection
            .get("config_generation")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        if generation != expected_generation {
            return Err(ProjectionError::bad_request(format!(
                "projection generation {generation} does not match the publication target {expected_generation}"
            )));
        }
        let computed = fingerprint(projection);
        if computed != expected_fingerprint {
            return Err(ProjectionError::bad_request(
                "projection fingerprint does not match the publication target",
            ));
        }
        if let Some((last_generation, last_fingerprint)) = &self.last_written {
            if generation < *last_generation {
                return Err(ProjectionError::bad_request(format!(
                    "projection generation regression refused (written {last_generation} > target {generation})"
                )));
            }
            if generation == *last_generation && computed != *last_fingerprint {
                return Err(ProjectionError::bad_request(
                    "same-generation projection content mismatch refused",
                ));
            }
        }
        let temp = self.path.with_file_name(format!(
            "{}.writer-tmp",
            self.path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("projection")
        ));
        let text = serde_json::to_string_pretty(projection)
            .map_err(|_| ProjectionError::bad_request("projection cannot be serialized"))?;
        std::fs::write(&temp, text)
            .map_err(|_| ProjectionError::bad_request("temp projection write failed"))?;
        let reread_text = std::fs::read_to_string(&temp)
            .map_err(|_| ProjectionError::bad_request("temp projection readback failed"))?;
        let reread: Value = serde_json::from_str(&reread_text)
            .map_err(|_| ProjectionError::bad_request("temp projection readback mismatch"))?;
        parse_projection_payload(&reread)?;
        if fingerprint(&reread) != computed {
            let _ = std::fs::remove_file(&temp);
            return Err(ProjectionError::bad_request(
                "temp projection readback mismatch",
            ));
        }
        if let Err(detail) = self.acl.check(&self.path) {
            let _ = std::fs::remove_file(&temp);
            return Err(ProjectionError::bad_request(detail));
        }
        std::fs::rename(&temp, &self.path).map_err(|_| {
            let _ = std::fs::remove_file(&temp);
            ProjectionError::bad_request("projection replace failed")
        })?;
        self.last_written = Some((generation, computed.clone()));
        Ok(computed)
    }

    /// Fingerprint one projection with the shared canonical encoder.
    pub fn fingerprint(&self, projection: &Value) -> String {
        fingerprint(projection)
    }
}
