//! Frozen Rust contract fixture.
//!
//! The fixture is produced by `tests/generate_rust_contract_fixture.py` from
//! `docs/rust-contract.md` and the Python modules that still own the behavior.
//! Rust code only *reads* the fixture; expected endpoints, error codes, SSE event
//! names and budgets are never written here by hand.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// Repository-relative path of the generated fixture.
pub const DEFAULT_FIXTURE_RELATIVE: &str = "tests/fixtures/rust_contract_rev1.json";

/// The typed, generated contract.
#[derive(Debug, Clone, Deserialize)]
pub struct ContractFixture {
    pub metadata: Metadata,
    pub ports: Ports,
    pub endpoints: Vec<Endpoint>,
    pub cloudflare: Cloudflare,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Metadata {
    pub fixture: bool,
    pub contract: String,
    pub contract_version: String,
    pub generated_from: String,
    pub source_hash_encoding: String,
    pub surfaces: Vec<String>,
    pub source_hashes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Ports {
    pub target: BTreeMap<String, u16>,
    pub production: Vec<u16>,
    pub legacy_aliases: Vec<u16>,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Endpoint {
    pub surface: String,
    pub method: String,
    pub path: String,
    pub auth: String,
    pub description: String,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Cloudflare {
    pub share_paths: Vec<String>,
    pub catch_all: String,
    pub read_only: bool,
    pub source: String,
}

/// A fixture load or validation failure.
#[derive(Debug)]
pub enum ContractError {
    Io(std::io::Error),
    Json(serde_json::Error),
    Invalid(String),
}

impl fmt::Display for ContractError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "cannot read contract fixture: {error}"),
            Self::Json(error) => write!(formatter, "cannot parse contract fixture: {error}"),
            Self::Invalid(reason) => write!(formatter, "invalid contract fixture: {reason}"),
        }
    }
}

impl std::error::Error for ContractError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Json(error) => Some(error),
            Self::Invalid(_) => None,
        }
    }
}

impl From<std::io::Error> for ContractError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<serde_json::Error> for ContractError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

impl ContractFixture {
    /// Load the generated fixture at an explicit path.
    pub fn load(path: impl AsRef<Path>) -> Result<Self, ContractError> {
        let text = fs::read_to_string(path)?;
        Ok(serde_json::from_str(&text)?)
    }

    /// Load the fixture at its repository-relative default path.
    pub fn load_default() -> Result<Self, ContractError> {
        Self::load(default_fixture_path())
    }

    /// Fail closed on a fixture that cannot describe the frozen contract.
    pub fn validate(&self) -> Result<(), ContractError> {
        if !self.metadata.fixture {
            return Err(ContractError::Invalid(
                "metadata.fixture must be true".to_string(),
            ));
        }
        if self.metadata.contract != "docs/rust-contract.md" {
            return Err(ContractError::Invalid(
                "metadata.contract must name docs/rust-contract.md".to_string(),
            ));
        }
        if self.metadata.source_hash_encoding != "utf8-lf" {
            return Err(ContractError::Invalid(
                "metadata.sourceHashEncoding must be utf8-lf".to_string(),
            ));
        }
        if self.metadata.surfaces.is_empty() {
            return Err(ContractError::Invalid(
                "metadata.surfaces must not be empty".to_string(),
            ));
        }
        if self.metadata.source_hashes.is_empty() {
            return Err(ContractError::Invalid(
                "metadata.sourceHashes must not be empty".to_string(),
            ));
        }
        for (name, hash) in &self.metadata.source_hashes {
            if hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                return Err(ContractError::Invalid(format!(
                    "source hash for {name} is not a SHA-256 hex digest"
                )));
            }
        }
        if self.endpoints.is_empty() {
            return Err(ContractError::Invalid(
                "endpoints must not be empty".to_string(),
            ));
        }

        let surfaces: BTreeSet<&str> = self.metadata.surfaces.iter().map(String::as_str).collect();
        let mut seen = BTreeSet::new();
        for endpoint in &self.endpoints {
            if !surfaces.contains(endpoint.surface.as_str()) {
                return Err(ContractError::Invalid(format!(
                    "endpoint surface is not declared in metadata: {}",
                    endpoint.surface
                )));
            }
            if !matches!(
                endpoint.method.as_str(),
                "GET" | "POST" | "PATCH" | "DELETE" | "OPTIONS" | "ANY"
            ) {
                return Err(ContractError::Invalid(format!(
                    "unsupported endpoint method: {}",
                    endpoint.method
                )));
            }
            if endpoint.path != "*" && !endpoint.path.starts_with('/') {
                return Err(ContractError::Invalid(format!(
                    "endpoint path is not absolute: {}",
                    endpoint.path
                )));
            }
            if !endpoint.source.starts_with(&self.metadata.contract) {
                return Err(ContractError::Invalid(format!(
                    "endpoint source does not cite the frozen contract: {}",
                    endpoint.source
                )));
            }
            let key = (
                endpoint.surface.clone(),
                endpoint.method.clone(),
                endpoint.path.clone(),
            );
            if !seen.insert(key) {
                return Err(ContractError::Invalid(format!(
                    "duplicate endpoint: {} {} {}",
                    endpoint.surface, endpoint.method, endpoint.path
                )));
            }
        }

        if self.ports.target.is_empty() {
            return Err(ContractError::Invalid(
                "ports.target must not be empty".to_string(),
            ));
        }
        let mut target_ports: Vec<u16> = self.ports.target.values().copied().collect();
        target_ports.sort_unstable();
        target_ports.dedup();
        if self.ports.production != target_ports {
            return Err(ContractError::Invalid(
                "ports.production must list the target ports".to_string(),
            ));
        }
        if !self.ports.legacy_aliases.is_empty() {
            return Err(ContractError::Invalid(
                "legacy port aliases must stay empty".to_string(),
            ));
        }

        if self.cloudflare.share_paths.is_empty() {
            return Err(ContractError::Invalid(
                "cloudflare.sharePaths must not be empty".to_string(),
            ));
        }
        if self.cloudflare.catch_all != "http_status:404" {
            return Err(ContractError::Invalid(
                "cloudflare.catchAll must be http_status:404".to_string(),
            ));
        }
        if !self.cloudflare.read_only {
            return Err(ContractError::Invalid(
                "cloudflare.readOnly must be true".to_string(),
            ));
        }
        for path in &self.cloudflare.share_paths {
            if !path.starts_with('/') {
                return Err(ContractError::Invalid(format!(
                    "share path is not absolute: {path}"
                )));
            }
            if !self.endpoints.iter().any(|endpoint| &endpoint.path == path) {
                return Err(ContractError::Invalid(format!(
                    "share path is missing from the endpoint table: {path}"
                )));
            }
        }
        Ok(())
    }

    /// Look up one endpoint by its surface, method and path.
    pub fn endpoint(&self, surface: &str, method: &str, path: &str) -> Option<&Endpoint> {
        self.endpoints.iter().find(|endpoint| {
            endpoint.surface == surface && endpoint.method == method && endpoint.path == path
        })
    }
}

/// Resolve the fixture path from this crate's compile-time location.
pub fn default_fixture_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../")
        .join(DEFAULT_FIXTURE_RELATIVE)
}
