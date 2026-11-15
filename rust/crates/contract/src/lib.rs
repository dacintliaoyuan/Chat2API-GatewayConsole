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
#[serde(rename_all = "camelCase")]
pub struct ContractFixture {
    pub metadata: Metadata,
    pub ports: Ports,
    pub endpoints: Vec<Endpoint>,
    pub errors: Vec<ErrorGroup>,
    pub sse: Sse,
    pub boundaries: Boundaries,
    pub broker: Broker,
    pub key_management: KeyManagement,
    pub metering: Metering,
    pub display: Display,
    pub share_auth: ShareAuth,
    pub cloudflare: Cloudflare,
    pub harness: Harness,
    pub coverage: BTreeMap<String, bool>,
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
pub struct ErrorGroup {
    pub surface: String,
    pub status: Option<u16>,
    pub codes: Vec<String>,
    pub messages: Vec<String>,
    pub description: String,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Sse {
    pub gateway: GatewaySse,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GatewaySse {
    pub chat: ChatSse,
    pub messages: MessagesSse,
    pub responses: ResponsesSse,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatSse {
    pub content_type: String,
    pub passthrough: bool,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MessagesSse {
    pub content_type: String,
    pub terminal_event: String,
    pub error_event: String,
    pub eof_before_terminal_is_success: bool,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResponsesSse {
    pub content_type: String,
    pub events: Vec<String>,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Boundaries {
    pub timeouts: Vec<Timeout>,
    pub cancellation: Vec<Cancellation>,
    pub idempotency: Vec<Idempotency>,
    pub retry: Retry,
    pub side_effects: Vec<SideEffect>,
    pub forbidden: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Timeout {
    pub surface: String,
    pub name: String,
    pub value: f64,
    pub unit: String,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Cancellation {
    pub surface: String,
    pub name: String,
    pub check: String,
    pub outcome: String,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Idempotency {
    pub surface: String,
    pub scope: String,
    pub key_min_length: Option<u32>,
    pub key_max_length: Option<u32>,
    pub reuse_status: Option<u16>,
    pub conflict_status: Option<u16>,
    pub conflict_code: Option<String>,
    pub replay_returns_plaintext: Option<bool>,
    pub one_shot: Option<bool>,
    pub acquire: Option<String>,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Retry {
    pub fast_failures_max: u32,
    pub slow_failure_threshold_seconds: f64,
    pub slow_failures_max: u32,
    pub physical_attempts_max: u32,
    pub retryable_statuses: Vec<u16>,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SideEffect {
    pub surface: String,
    pub kind: String,
    pub target: Option<String>,
    pub blocking: bool,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Broker {
    pub schema_version: u32,
    pub max_message_bytes: u64,
    pub ts_window_seconds: u64,
    pub default_deadline_seconds: f64,
    pub drain_window_seconds: f64,
    pub stop_budget_seconds: f64,
    pub drain_deadline_seconds: f64,
    pub emergency_deadline_seconds: f64,
    pub dispatch_deadline_seconds: f64,
    pub management_actions: Vec<String>,
    pub instance_actions: Vec<String>,
    pub result_codes: Vec<String>,
    pub management_signed_keys: Vec<String>,
    pub instance_signed_keys: Vec<String>,
    pub request_id_one_shot: bool,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct KeyManagement {
    pub schema_version: u32,
    pub projection_schema_version: u32,
    pub key_concurrency: Concurrency,
    pub share_concurrency: ShareConcurrency,
    pub idempotency_key_min_length: u32,
    pub idempotency_key_max_length: u32,
    pub plaintext_returned_once: bool,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Concurrency {
    pub default: u32,
    pub min: u32,
    pub max: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ShareConcurrency {
    pub new_deployment: u32,
    pub legacy: u32,
    pub min: u32,
    pub max: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Metering {
    pub usage_sources: Vec<String>,
    pub cache_report_statuses: Vec<String>,
    pub complete_status: String,
    pub request_results: Vec<String>,
    pub windows: Vec<String>,
    pub page_min: u32,
    pub page_default: u32,
    pub page_max: u32,
    pub ledger_limit: u64,
    pub private_ledger_basenames: Vec<String>,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Display {
    pub grant_ttl_seconds: f64,
    pub session_ttl_seconds: f64,
    pub max_pending_grants: u32,
    pub max_sessions: u32,
    pub max_connections: u32,
    pub idle_timeout_seconds: f64,
    pub max_session_seconds: f64,
    pub handshake_timeout_seconds: f64,
    pub close_grace_seconds: f64,
    pub max_grant_body_bytes: u32,
    pub max_grant_token_chars: u32,
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ShareAuth {
    pub messages_path: String,
    pub chat_fields: Vec<String>,
    pub messages_fields: Vec<String>,
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

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Harness {
    pub bind_host: String,
    pub bind_port: u16,
    pub temp_dir_prefix: String,
    pub production_ports: Vec<u16>,
    pub production_data_roots: Vec<String>,
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

        if self.errors.is_empty() {
            return Err(ContractError::Invalid(
                "errors must not be empty".to_string(),
            ));
        }
        for group in &self.errors {
            if !group.source.starts_with(&self.metadata.contract) && !group.source.contains(".py:")
            {
                return Err(ContractError::Invalid(format!(
                    "error group source has no contract or Python citation: {}",
                    group.source
                )));
            }
            if group.codes.is_empty() && group.messages.is_empty() && group.description.is_empty() {
                return Err(ContractError::Invalid(format!(
                    "error group has neither codes nor a description: {} {}",
                    group.surface,
                    group.status.map_or(0, |status| status)
                )));
            }
            let unique: BTreeSet<&str> = group.codes.iter().map(String::as_str).collect();
            if unique.len() != group.codes.len() {
                return Err(ContractError::Invalid(format!(
                    "error group has duplicate codes: {}",
                    group.surface
                )));
            }
        }

        if self.sse.gateway.responses.events.is_empty() {
            return Err(ContractError::Invalid(
                "Responses SSE events must not be empty".to_string(),
            ));
        }
        let events: BTreeSet<&str> = self
            .sse
            .gateway
            .responses
            .events
            .iter()
            .map(String::as_str)
            .collect();
        if events.len() != self.sse.gateway.responses.events.len() {
            return Err(ContractError::Invalid(
                "Responses SSE events must be unique".to_string(),
            ));
        }
        if !events.iter().all(|event| event.starts_with("response.")) {
            return Err(ContractError::Invalid(
                "Responses SSE event names must start with response.".to_string(),
            ));
        }
        if self.sse.gateway.messages.terminal_event.is_empty()
            || self.sse.gateway.messages.error_event.is_empty()
        {
            return Err(ContractError::Invalid(
                "Messages SSE terminal and error events must be present".to_string(),
            ));
        }

        if self.boundaries.timeouts.is_empty()
            || self.boundaries.cancellation.is_empty()
            || self.boundaries.idempotency.is_empty()
            || self.boundaries.side_effects.is_empty()
            || self.boundaries.forbidden.is_empty()
        {
            return Err(ContractError::Invalid(
                "boundary coverage must not be empty".to_string(),
            ));
        }
        for timeout in &self.boundaries.timeouts {
            if !timeout.value.is_finite() || timeout.value <= 0.0 {
                return Err(ContractError::Invalid(format!(
                    "timeout is not a positive finite budget: {} {}",
                    timeout.surface, timeout.name
                )));
            }
            if !matches!(
                timeout.unit.as_str(),
                "seconds" | "bytes" | "entries" | "concurrent"
            ) {
                return Err(ContractError::Invalid(format!(
                    "timeout has an unknown unit: {}",
                    timeout.unit
                )));
            }
            if timeout.source.is_empty() {
                return Err(ContractError::Invalid("timeout has no source".to_string()));
            }
        }
        for item in &self.boundaries.cancellation {
            if item.name.is_empty() || item.outcome.is_empty() || item.source.is_empty() {
                return Err(ContractError::Invalid(
                    "cancellation boundary is incomplete".to_string(),
                ));
            }
        }
        for item in &self.boundaries.idempotency {
            if item.scope.is_empty() || item.source.is_empty() {
                return Err(ContractError::Invalid(
                    "idempotency boundary is incomplete".to_string(),
                ));
            }
            if let (Some(min), Some(max)) = (item.key_min_length, item.key_max_length) {
                if min == 0 || min > max {
                    return Err(ContractError::Invalid(
                        "idempotency key bounds are invalid".to_string(),
                    ));
                }
            }
        }
        if self.boundaries.retry.retryable_statuses.is_empty()
            || self.boundaries.retry.fast_failures_max == 0
            || self.boundaries.retry.slow_failures_max == 0
            || self.boundaries.retry.physical_attempts_max == 0
        {
            return Err(ContractError::Invalid(
                "retry boundary is incomplete".to_string(),
            ));
        }
        for item in &self.boundaries.side_effects {
            if item.kind.is_empty() || item.source.is_empty() {
                return Err(ContractError::Invalid(
                    "side-effect boundary is incomplete".to_string(),
                ));
            }
        }

        if self.broker.schema_version == 0
            || self.broker.max_message_bytes == 0
            || self.broker.ts_window_seconds == 0
            || self.broker.default_deadline_seconds <= 0.0
            || self.broker.drain_window_seconds <= 0.0
            || self.broker.stop_budget_seconds <= 0.0
            || self.broker.drain_deadline_seconds <= 0.0
            || self.broker.emergency_deadline_seconds <= 0.0
            || self.broker.dispatch_deadline_seconds < self.broker.drain_deadline_seconds
        {
            return Err(ContractError::Invalid(
                "broker budgets are inconsistent".to_string(),
            ));
        }
        if self.broker.management_actions.is_empty()
            || self.broker.instance_actions.is_empty()
            || self.broker.result_codes.is_empty()
            || self.broker.management_signed_keys.is_empty()
            || self.broker.instance_signed_keys.is_empty()
        {
            return Err(ContractError::Invalid(
                "broker actions or result codes are empty".to_string(),
            ));
        }
        let management: BTreeSet<&str> = self
            .broker
            .management_actions
            .iter()
            .map(String::as_str)
            .collect();
        let instance: BTreeSet<&str> = self
            .broker
            .instance_actions
            .iter()
            .map(String::as_str)
            .collect();
        if management.len() != self.broker.management_actions.len()
            || instance.len() != self.broker.instance_actions.len()
        {
            return Err(ContractError::Invalid(
                "broker action lists contain duplicates".to_string(),
            ));
        }
        if !self.broker.request_id_one_shot {
            return Err(ContractError::Invalid(
                "broker request_id must be one-shot".to_string(),
            ));
        }

        if self.key_management.schema_version == 0
            || self.key_management.projection_schema_version == 0
            || self.key_management.key_concurrency.min == 0
            || self.key_management.key_concurrency.min > self.key_management.key_concurrency.max
            || self.key_management.key_concurrency.default < self.key_management.key_concurrency.min
            || self.key_management.key_concurrency.default > self.key_management.key_concurrency.max
            || self.key_management.share_concurrency.min == 0
            || self.key_management.share_concurrency.min > self.key_management.share_concurrency.max
            || self.key_management.share_concurrency.new_deployment
                < self.key_management.share_concurrency.min
            || self.key_management.share_concurrency.new_deployment
                > self.key_management.share_concurrency.max
            || self.key_management.idempotency_key_min_length == 0
            || self.key_management.idempotency_key_min_length
                > self.key_management.idempotency_key_max_length
        {
            return Err(ContractError::Invalid(
                "key-management bounds are inconsistent".to_string(),
            ));
        }
        if !self.key_management.plaintext_returned_once {
            return Err(ContractError::Invalid(
                "key plaintext must be returned once".to_string(),
            ));
        }

        if self.metering.usage_sources.is_empty()
            || self.metering.cache_report_statuses.is_empty()
            || self.metering.request_results.is_empty()
            || self.metering.windows.is_empty()
            || self.metering.private_ledger_basenames.is_empty()
            || self.metering.complete_status.is_empty()
        {
            return Err(ContractError::Invalid(
                "metering vocabulary is incomplete".to_string(),
            ));
        }
        if self.metering.page_min == 0
            || self.metering.page_min > self.metering.page_default
            || self.metering.page_default > self.metering.page_max
            || self.metering.ledger_limit == 0
        {
            return Err(ContractError::Invalid(
                "metering page or ledger bounds are inconsistent".to_string(),
            ));
        }

        if self.display.grant_ttl_seconds <= 0.0
            || self.display.session_ttl_seconds <= 0.0
            || self.display.idle_timeout_seconds <= 0.0
            || self.display.max_session_seconds <= 0.0
            || self.display.handshake_timeout_seconds <= 0.0
            || self.display.close_grace_seconds <= 0.0
            || self.display.max_pending_grants == 0
            || self.display.max_sessions == 0
            || self.display.max_connections == 0
            || self.display.max_grant_body_bytes == 0
            || self.display.max_grant_token_chars == 0
        {
            return Err(ContractError::Invalid(
                "display budgets are inconsistent".to_string(),
            ));
        }

        if !self.share_auth.messages_path.starts_with('/')
            || self.share_auth.chat_fields.is_empty()
            || self.share_auth.messages_fields.is_empty()
        {
            return Err(ContractError::Invalid(
                "share-auth request fields are incomplete".to_string(),
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

        if self.harness.bind_host != "127.0.0.1" || self.harness.bind_port != 0 {
            return Err(ContractError::Invalid(
                "harness must bind an ephemeral loopback port".to_string(),
            ));
        }
        if self.harness.temp_dir_prefix.is_empty()
            || self.harness.production_data_roots.is_empty()
            || self.harness.production_ports != self.ports.production
        {
            return Err(ContractError::Invalid(
                "harness isolation metadata is incomplete".to_string(),
            ));
        }

        if self.coverage.is_empty() || !self.coverage.values().all(|covered| *covered) {
            return Err(ContractError::Invalid(
                "fixture coverage must be complete".to_string(),
            ));
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
