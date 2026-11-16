//! Gateway configuration and share-projection parsing.
//!
//! The projection parser is a Rust port of
//! `share/broker_protocol.py::parse_projection_payload` plus
//! `chat2api/share_auth.py::load_config`: schema 1 and 2 are accepted, missing
//! limits fall back to the legacy effective values, old-contract values are
//! clamped to the new caps, and unknown fields are rejected. The legacy
//! `{"clients": ...}` format stays accepted only outside projection mode.

use std::collections::BTreeSet;
use std::fs;
use std::net::IpAddr;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;

pub const LEGACY_KEY_MAX_CONCURRENCY: u32 = 1;
pub const LEGACY_SHARE_MAX_CONCURRENCY: u32 = 2;
pub const PROJECTION_DEFAULT_MAX_CONCURRENCY: u32 = 1;
pub const PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY: u32 = 2;
pub const PROJECTION_MAX_CONCURRENCY_MIN: u32 = 1;
pub const PROJECTION_MAX_CONCURRENCY_MAX: u32 = 48;
pub const PROJECTION_LEGACY_MAX_CONCURRENCY_MAX: u32 = 64;
pub const PROJECTION_SHARE_MAX_CONCURRENCY_MIN: u32 = 1;
pub const PROJECTION_SHARE_MAX_CONCURRENCY_MAX: u32 = 64;
pub const PROJECTION_LEGACY_SHARE_MAX_CONCURRENCY_MAX: u32 = 128;
pub const DEFAULT_SHARE_MAX_REQUEST_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_SHARE_REQUEST_BYTES: u64 = 1024 * 1024 * 1024;
pub const CONSOLE_MAX_REQUEST_BYTES: u64 = 65536;
pub const DEFAULT_UPSTREAM_TIMEOUT: Duration = Duration::from_secs(15);
/// A request body must finish within this budget; a stalled body gets 408.
pub const DEFAULT_BODY_READ_TIMEOUT_SECONDS: u64 = 60;
/// The total request budget includes hidden retries and authentication waits;
/// spec #58 raised it from 360 to 540 seconds so a full retry cycle still fits.
pub const DEFAULT_REQUEST_TOTAL_SECONDS: u64 = 540;
pub const MIN_REQUEST_TOTAL_SECONDS: u64 = 10;
pub const MAX_REQUEST_TOTAL_SECONDS: u64 = 3600;
/// The cumulative authentication wait of one logical request (#63).
pub const DEFAULT_AUTH_WAIT_BUDGET_SECONDS: f64 = 30.0;
pub const MIN_AUTH_WAIT_BUDGET_SECONDS: f64 = 0.01;
pub const MAX_AUTH_WAIT_BUDGET_SECONDS: f64 = 180.0;
/// Concurrent authentication waiters, not login sessions.
pub const DEFAULT_AUTH_WAIT_MAX_CONCURRENT: usize = 128;
pub const MIN_AUTH_WAIT_MAX_CONCURRENT: usize = 1;
pub const MAX_AUTH_WAIT_MAX_CONCURRENT: usize = 1024;
/// The authentication wait rechecks the shared session at this interval.
pub const DEFAULT_AUTH_WAIT_RECHECK_SECONDS: f64 = 1.0;
pub const MIN_AUTH_WAIT_RECHECK_SECONDS: f64 = 0.01;
pub const MAX_AUTH_WAIT_RECHECK_SECONDS: f64 = 1.0;
/// The rolling global sign-in start window and cap (#63).
pub const DEFAULT_REAUTH_ATTEMPT_WINDOW_SECONDS: u64 = 900;
pub const DEFAULT_REAUTH_MAX_ATTEMPTS_PER_WINDOW: usize = 3;
pub const DEFAULT_FIRST_BYTE_TIMEOUT: Duration = Duration::from_secs(120);
pub const DEFAULT_SOCKET_SILENCE_TIMEOUT: Duration = Duration::from_secs(180);
pub const DEFAULT_MAX_FAST_RETRIES: u32 = 5;
pub const DEFAULT_MAX_SLOW_RETRIES: u32 = 2;
pub const DEFAULT_MAX_PHYSICAL_ATTEMPTS: u32 = 12;
pub const DEFAULT_RETRY_DELAY_BASE: Duration = Duration::from_millis(500);
pub const DEFAULT_SLOW_RETRY_THRESHOLD: Duration = Duration::from_secs(30);

/// Share-configuration format fixed at instance start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthMode {
    Auto,
    Projection,
    Legacy,
}

impl AuthMode {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "projection" => Some(Self::Projection),
            "legacy" => Some(Self::Legacy),
            _ => None,
        }
    }
}

/// One normalized share-key entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShareEntry {
    pub key_id: String,
    pub name: String,
    pub secret_hash: String,
    pub secret_version: u32,
    pub enabled: bool,
    pub max_concurrency: u32,
}

/// The trusted `(key, model, protocol)` scope of the non-stream aggregate
/// transport. An empty scope is the explicit disabled state; a partially empty
/// object is ambiguous and never parses.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct AggregateScope {
    pub key_ids: BTreeSet<String>,
    pub models: BTreeSet<String>,
    pub protocols: BTreeSet<String>,
}

const NONSTREAM_AGGREGATE_PROTOCOLS: [&str; 3] = ["chat", "messages", "responses"];
const NONSTREAM_AGGREGATE_SCOPE_MAX_ENTRIES: usize = 100;
const NONSTREAM_AGGREGATE_SCOPE_MAX_CHARS: usize = 256;
const NONSTREAM_AGGREGATE_SCOPE_MAX_RAW: usize = 65536;

/// Python `parse_nonstream_aggregate_scope`: `None` on ANY failure.
pub fn parse_nonstream_aggregate_scope(raw: Option<&str>) -> Option<AggregateScope> {
    let raw = raw.filter(|raw| !raw.trim().is_empty())?;
    if raw.len() > NONSTREAM_AGGREGATE_SCOPE_MAX_RAW {
        return None;
    }
    let data: Value = serde_json::from_str(raw).ok()?;
    let data = data.as_object()?;
    let fields: BTreeSet<&str> = ["key_ids", "models", "protocols"].into_iter().collect();
    if data.keys().map(String::as_str).collect::<BTreeSet<_>>() != fields {
        return None;
    }
    let mut parsed = AggregateScope::default();
    for name in ["key_ids", "models", "protocols"] {
        let values = data.get(name)?.as_array()?;
        if values.len() > NONSTREAM_AGGREGATE_SCOPE_MAX_ENTRIES {
            return None;
        }
        let mut normalized = BTreeSet::new();
        for value in values {
            let value = value.as_str()?;
            if value.is_empty()
                || value.len() > NONSTREAM_AGGREGATE_SCOPE_MAX_CHARS
                || value.trim() != value
            {
                return None;
            }
            if !normalized.insert(value.to_string()) {
                return None;
            }
        }
        match name {
            "key_ids" => parsed.key_ids = normalized,
            "models" => parsed.models = normalized,
            _ => parsed.protocols = normalized,
        }
    }
    if parsed
        .protocols
        .iter()
        .any(|protocol| !NONSTREAM_AGGREGATE_PROTOCOLS.contains(&protocol.as_str()))
    {
        return None;
    }
    let empty = ["key_ids", "models", "protocols"]
        .iter()
        .filter(|name| match **name {
            "key_ids" => parsed.key_ids.is_empty(),
            "models" => parsed.models.is_empty(),
            _ => parsed.protocols.is_empty(),
        })
        .count();
    if empty != 0 && empty != 3 {
        return None;
    }
    Some(parsed)
}

/// The normalized share configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShareConfig {
    pub schema_version: u32,
    pub config_generation: u64,
    pub share_max_concurrency: u32,
    pub legacy: bool,
    pub entries: Vec<ShareEntry>,
}

/// How the gateway chooses its listening socket.
///
/// Candidate is the default and only binds an ephemeral loopback port for parity
/// tests. Production must be selected explicitly and only binds 8003/8002 on
/// loopback; both modes share the same routes, authentication, ledger and error
/// contracts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum BindMode {
    #[default]
    Candidate,
    Production,
}

impl BindMode {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "candidate" => Some(Self::Candidate),
            "production" => Some(Self::Production),
            _ => None,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Candidate => "candidate",
            Self::Production => "production",
        }
    }
}

/// One gateway configuration.
#[derive(Debug, Clone)]
pub struct GatewayConfig {
    pub bind_host: String,
    pub bind_port: u16,
    pub upstream_base_url: String,
    pub upstream_token: String,
    pub share_config: Option<PathBuf>,
    pub auth_mode: AuthMode,
    pub allowed_origins: BTreeSet<String>,
    pub control_token_file: Option<PathBuf>,
    pub local_api_key: String,
    pub share_max_request_bytes: u64,
    pub max_request_bytes: u64,
    pub local_max_request_bytes: u64,
    pub upstream_timeout: Duration,
    /// The R13 non-stream aggregate switch (off by default).
    pub nonstream_upstream_aggregate: bool,
    /// The trusted startup scope; `None` keeps every request native.
    pub nonstream_aggregate_scope: Option<AggregateScope>,
    /// Share ledger path; `None` keeps the candidate from writing any ledger.
    pub usage_log: Option<PathBuf>,
    /// Runtime event log path (`CHAT2API_SERVICE_LOG`); `None` keeps the
    /// candidate from writing a production log file.
    pub service_log: Option<PathBuf>,
    /// Request-body read budget; a stalled body gets 408 after this.
    pub body_read_timeout: Duration,
    /// Total wall-clock budget of one Chat request.
    pub request_total: Duration,
    /// Cumulative authentication wait of one logical request (#63).
    pub auth_wait_budget: Duration,
    /// Concurrent authentication waiters (#63), not login sessions.
    pub auth_wait_max_concurrent: usize,
    /// How often a waiting request rechecks the shared session.
    pub auth_wait_recheck: Duration,
    /// Rolling global sign-in start window.
    pub reauth_attempt_window: Duration,
    /// Global sign-in starts allowed inside one rolling window.
    pub reauth_max_attempts_per_window: usize,
    /// The explicit `--no-browser` mode; a real login provider may ignore it.
    pub browser_disabled: bool,
    /// First substantive SSE event budget.
    pub first_byte_timeout: Duration,
    /// One upstream socket-read silence budget.
    pub socket_silence_timeout: Duration,
    pub max_fast_retries: u32,
    pub max_slow_retries: u32,
    pub max_physical_attempts: u32,
    pub retry_delay_base: Duration,
    pub slow_retry_threshold: Duration,
    /// Candidate mode (the default); `false` selects the explicit production
    /// binding policy.
    pub candidate: bool,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        Self {
            bind_host: "127.0.0.1".to_string(),
            bind_port: 0,
            upstream_base_url: String::new(),
            upstream_token: String::new(),
            share_config: None,
            auth_mode: AuthMode::Auto,
            allowed_origins: BTreeSet::new(),
            control_token_file: None,
            local_api_key: String::new(),
            share_max_request_bytes: DEFAULT_SHARE_MAX_REQUEST_BYTES,
            max_request_bytes: DEFAULT_SHARE_MAX_REQUEST_BYTES,
            local_max_request_bytes: CONSOLE_MAX_REQUEST_BYTES,
            upstream_timeout: DEFAULT_UPSTREAM_TIMEOUT,
            nonstream_upstream_aggregate: false,
            nonstream_aggregate_scope: None,
            usage_log: None,
            service_log: None,
            body_read_timeout: Duration::from_secs(DEFAULT_BODY_READ_TIMEOUT_SECONDS),
            request_total: Duration::from_secs(DEFAULT_REQUEST_TOTAL_SECONDS),
            auth_wait_budget: Duration::from_secs_f64(DEFAULT_AUTH_WAIT_BUDGET_SECONDS),
            auth_wait_max_concurrent: DEFAULT_AUTH_WAIT_MAX_CONCURRENT,
            auth_wait_recheck: Duration::from_secs_f64(DEFAULT_AUTH_WAIT_RECHECK_SECONDS),
            reauth_attempt_window: Duration::from_secs(DEFAULT_REAUTH_ATTEMPT_WINDOW_SECONDS),
            reauth_max_attempts_per_window: DEFAULT_REAUTH_MAX_ATTEMPTS_PER_WINDOW,
            browser_disabled: false,
            first_byte_timeout: DEFAULT_FIRST_BYTE_TIMEOUT,
            socket_silence_timeout: DEFAULT_SOCKET_SILENCE_TIMEOUT,
            max_fast_retries: DEFAULT_MAX_FAST_RETRIES,
            max_slow_retries: DEFAULT_MAX_SLOW_RETRIES,
            max_physical_attempts: DEFAULT_MAX_PHYSICAL_ATTEMPTS,
            retry_delay_base: DEFAULT_RETRY_DELAY_BASE,
            slow_retry_threshold: DEFAULT_SLOW_RETRY_THRESHOLD,
            candidate: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for ConfigError {}

pub fn is_loopback_host(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    if host == "localhost" {
        return true;
    }
    host.parse::<IpAddr>()
        .map(|address| address.is_loopback())
        .unwrap_or(false)
}

/// Ports that the candidate must never bind while it is a candidate.
pub fn governed_ports() -> BTreeSet<u16> {
    [5173, 8000, 8001, 8002, 8003, 8097, 8099]
        .into_iter()
        .collect()
}

/// The only ports production mode may bind.
pub fn production_ports() -> BTreeSet<u16> {
    [8002, 8003].into_iter().collect()
}

impl GatewayConfig {
    pub fn from_env() -> Self {
        let mut config = Self::default();
        if let Ok(value) = std::env::var("CHAT2API_ALLOWED_ORIGINS") {
            config.allowed_origins = value
                .split(',')
                .map(str::trim)
                .filter(|origin| !origin.is_empty())
                .map(str::to_string)
                .collect();
        }
        if let Ok(value) = std::env::var("CHAT2API_UPSTREAM_TOKEN") {
            config.upstream_token = value.trim().to_string();
        }
        if let Ok(value) = std::env::var("CHAT2API_LOCAL_API_KEY") {
            config.local_api_key = value.trim().to_string();
        }
        if let Ok(value) = std::env::var("CHAT2API_SHARE_MAX_REQUEST_BYTES") {
            if let Ok(parsed) = value.parse::<u64>() {
                config.share_max_request_bytes = parsed.clamp(1, MAX_SHARE_REQUEST_BYTES);
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_MAX_REQUEST_BYTES") {
            if let Ok(parsed) = value.parse::<u64>() {
                config.max_request_bytes = parsed.clamp(1, MAX_SHARE_REQUEST_BYTES);
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_CONTROL_TOKEN_FILE") {
            if !value.trim().is_empty() {
                config.control_token_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_USAGE_LOG") {
            if !value.trim().is_empty() {
                config.usage_log = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_SERVICE_LOG") {
            if !value.trim().is_empty() {
                config.service_log = Some(PathBuf::from(value));
            }
        }
        config.request_total = parse_request_total_seconds(
            std::env::var("CHAT2API_MAX_REQUEST_SECONDS")
                .ok()
                .as_deref(),
        );
        config.auth_wait_budget =
            parse_auth_wait_budget(std::env::var("CHAT2API_AUTH_WAIT_BUDGET_S").ok().as_deref());
        config.auth_wait_max_concurrent = parse_auth_wait_max_concurrent(
            std::env::var("CHAT2API_AUTH_WAIT_MAX_CONCURRENT")
                .ok()
                .as_deref(),
        );
        config.auth_wait_recheck = parse_auth_wait_recheck(
            std::env::var("CHAT2API_AUTH_WAIT_RECHECK_S")
                .ok()
                .as_deref(),
        );
        config.browser_disabled = std::env::var("CHAT2API_NO_BROWSER")
            .ok()
            .is_some_and(|value| matches!(value.trim(), "1" | "true" | "yes"));
        config.nonstream_upstream_aggregate =
            std::env::var("CHAT2API_NONSTREAM_UPSTREAM_AGGREGATE")
                .ok()
                .is_some_and(|value| matches!(value.trim(), "1" | "true" | "yes"));
        config.nonstream_aggregate_scope = parse_nonstream_aggregate_scope(
            std::env::var("CHAT2API_NONSTREAM_AGGREGATE_SCOPE")
                .ok()
                .as_deref(),
        );
        config
    }

    /// The selected binding policy. Candidate is the default.
    pub fn bind_mode(&self) -> BindMode {
        if self.candidate {
            BindMode::Candidate
        } else {
            BindMode::Production
        }
    }

    /// Fail closed before binding a socket.
    pub fn validate_bind_policy(&self) -> Result<(), ConfigError> {
        if !is_loopback_host(&self.bind_host) {
            return Err(ConfigError(format!(
                "{} bind host is not loopback: {}",
                self.bind_mode().label(),
                self.bind_host
            )));
        }
        if self.candidate {
            if self.bind_port != 0 {
                return Err(ConfigError(format!(
                    "candidate must bind an ephemeral loopback port, got {}",
                    self.bind_port
                )));
            }
            if governed_ports().contains(&self.bind_port) {
                return Err(ConfigError(format!(
                    "refusing to bind governed production port {}",
                    self.bind_port
                )));
            }
        } else if !production_ports().contains(&self.bind_port) {
            return Err(ConfigError(format!(
                "production must bind one of 8002 or 8003 on loopback, got {}",
                self.bind_port
            )));
        }
        if !(1..=MAX_SHARE_REQUEST_BYTES).contains(&self.share_max_request_bytes) {
            return Err(ConfigError(
                "share request body limit must be 1..1073741824".to_string(),
            ));
        }
        if !(1..=MAX_SHARE_REQUEST_BYTES).contains(&self.max_request_bytes) {
            return Err(ConfigError(
                "request body limit must be 1..1073741824".to_string(),
            ));
        }
        if self.local_max_request_bytes == 0
            || self.local_max_request_bytes > CONSOLE_MAX_REQUEST_BYTES
        {
            return Err(ConfigError(
                "console request body limit must be 1..65536".to_string(),
            ));
        }
        parse_upstream_base_url(&self.upstream_base_url)?;
        Ok(())
    }
}

/// Parse the total request budget; invalid values keep the default and valid
/// values are clamped to 10..3600 seconds, exactly like Python.
pub fn parse_request_total_seconds(value: Option<&str>) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<i64>().ok())
        .unwrap_or(DEFAULT_REQUEST_TOTAL_SECONDS as i64);
    Duration::from_secs(seconds.clamp(
        MIN_REQUEST_TOTAL_SECONDS as i64,
        MAX_REQUEST_TOTAL_SECONDS as i64,
    ) as u64)
}

/// Parse the per-request cumulative authentication-wait budget.
pub fn parse_auth_wait_budget(value: Option<&str>) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
        .unwrap_or(DEFAULT_AUTH_WAIT_BUDGET_SECONDS);
    Duration::from_secs_f64(
        seconds.clamp(MIN_AUTH_WAIT_BUDGET_SECONDS, MAX_AUTH_WAIT_BUDGET_SECONDS),
    )
}

/// Parse the concurrent authentication-wait slot count.
pub fn parse_auth_wait_max_concurrent(value: Option<&str>) -> usize {
    let count = value
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(DEFAULT_AUTH_WAIT_MAX_CONCURRENT);
    count.clamp(MIN_AUTH_WAIT_MAX_CONCURRENT, MAX_AUTH_WAIT_MAX_CONCURRENT)
}

/// Parse the authentication-wait recheck interval.
pub fn parse_auth_wait_recheck(value: Option<&str>) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
        .unwrap_or(DEFAULT_AUTH_WAIT_RECHECK_SECONDS);
    Duration::from_secs_f64(
        seconds.clamp(MIN_AUTH_WAIT_RECHECK_SECONDS, MAX_AUTH_WAIT_RECHECK_SECONDS),
    )
}

/// Parse and validate the upstream base URL. Only plain HTTP is supported by the
/// S1a candidate; TLS termination is a later deployment slice.
pub fn parse_upstream_base_url(value: &str) -> Result<(String, u16), ConfigError> {
    let rest = value.strip_prefix("http://").ok_or_else(|| {
        ConfigError(format!(
            "upstream base URL must be http:// for the S1a candidate: {value}"
        ))
    })?;
    let rest = rest.strip_suffix('/').unwrap_or(rest);
    if rest.is_empty() || rest.contains('/') {
        return Err(ConfigError(format!(
            "upstream base URL must name only host and port: {value}"
        )));
    }
    let (host, port) = match rest.rsplit_once(':') {
        Some((host, port)) => {
            let port = port
                .parse::<u16>()
                .map_err(|_| ConfigError(format!("invalid upstream port in {value}")))?;
            (host, port)
        }
        None => (rest, 80),
    };
    if host.is_empty() || host.contains('@') {
        return Err(ConfigError(format!(
            "upstream base URL must not carry credentials: {value}"
        )));
    }
    Ok((host.to_string(), port))
}

/// Parse one share configuration document.
pub fn parse_share_config(document: &str, auth_mode: AuthMode) -> Result<ShareConfig, ConfigError> {
    let value: Value = serde_json::from_str(document)
        .map_err(|_| ConfigError("Invalid sharing configuration".to_string()))?;
    parse_share_config_value(&value, auth_mode)
}

/// Parse one already-decoded share configuration value.
pub fn parse_share_config_value(
    value: &Value,
    auth_mode: AuthMode,
) -> Result<ShareConfig, ConfigError> {
    let object = value
        .as_object()
        .ok_or_else(|| ConfigError("Invalid sharing configuration".to_string()))?;
    let projection_markers = ["schema_version", "config_generation", "data"];

    if object.contains_key("clients") {
        if auth_mode == AuthMode::Projection {
            return Err(ConfigError(
                "Legacy client configuration is not allowed in projection mode".to_string(),
            ));
        }
        if projection_markers
            .iter()
            .any(|marker| object.contains_key(*marker))
        {
            return Err(ConfigError(
                "Mixed sharing configuration format".to_string(),
            ));
        }
        if object.len() != 1 {
            return Err(ConfigError(
                "Legacy configuration has unknown fields".to_string(),
            ));
        }
        let clients = object
            .get("clients")
            .and_then(Value::as_object)
            .ok_or_else(|| ConfigError("Invalid sharing configuration".to_string()))?;
        let mut entries = Vec::new();
        for (name, digest) in clients {
            if name.is_empty() {
                return Err(ConfigError("Invalid client name".to_string()));
            }
            let digest = digest
                .as_str()
                .ok_or_else(|| ConfigError("Invalid digest".to_string()))?;
            validate_digest(digest)?;
            entries.push(ShareEntry {
                key_id: name.clone(),
                name: name.clone(),
                secret_hash: digest.to_string(),
                secret_version: 1,
                enabled: true,
                max_concurrency: LEGACY_KEY_MAX_CONCURRENCY,
            });
        }
        return Ok(ShareConfig {
            schema_version: 1,
            config_generation: 0,
            share_max_concurrency: LEGACY_SHARE_MAX_CONCURRENCY,
            legacy: true,
            entries,
        });
    }

    if projection_markers
        .iter()
        .any(|marker| object.contains_key(*marker))
    {
        if auth_mode == AuthMode::Legacy {
            return Err(ConfigError(
                "Projection configuration is not allowed in legacy mode".to_string(),
            ));
        }
        return parse_projection(object);
    }

    Err(ConfigError("Unsupported sharing configuration".to_string()))
}

fn parse_projection(object: &serde_json::Map<String, Value>) -> Result<ShareConfig, ConfigError> {
    let allowed: BTreeSet<&str> = [
        "schema_version",
        "config_generation",
        "data",
        "share_max_concurrency",
    ]
    .into_iter()
    .collect();
    if object.keys().any(|key| !allowed.contains(key.as_str())) {
        return Err(ConfigError("projection has unknown fields".to_string()));
    }
    let schema = object
        .get("schema_version")
        .and_then(Value::as_u64)
        .ok_or_else(|| ConfigError("unsupported projection schema".to_string()))?;
    if schema != 1 && schema != 2 {
        return Err(ConfigError("unsupported projection schema".to_string()));
    }
    let generation = object
        .get("config_generation")
        .and_then(Value::as_u64)
        .ok_or_else(|| ConfigError("invalid projection generation".to_string()))?;
    let data = object
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| ConfigError("invalid projection data".to_string()))?;
    let share_max = match object.get("share_max_concurrency") {
        None | Some(Value::Null) => PROJECTION_DEFAULT_SHARE_MAX_CONCURRENCY,
        Some(value) => parse_projection_limit(
            value,
            "share_max_concurrency",
            PROJECTION_SHARE_MAX_CONCURRENCY_MIN,
            PROJECTION_SHARE_MAX_CONCURRENCY_MAX,
            PROJECTION_LEGACY_SHARE_MAX_CONCURRENCY_MAX,
        )?,
    };
    let mut seen_ids = BTreeSet::new();
    let mut seen_names = BTreeSet::new();
    let mut entries = Vec::new();
    for item in data {
        let item = item
            .as_object()
            .ok_or_else(|| ConfigError("invalid projection entry".to_string()))?;
        let allowed_entry: BTreeSet<&str> = [
            "key_id",
            "name",
            "secret_hash",
            "secret_version",
            "enabled",
            "max_concurrency",
        ]
        .into_iter()
        .collect();
        if item.keys().any(|key| !allowed_entry.contains(key.as_str())) {
            return Err(ConfigError(
                "projection entry has unknown field(s)".to_string(),
            ));
        }
        let key_id = item
            .get("key_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ConfigError("invalid projection identity".to_string()))?;
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ConfigError("invalid projection identity".to_string()))?;
        if !seen_ids.insert(key_id.to_string()) || !seen_names.insert(name.to_string()) {
            return Err(ConfigError("duplicate projection identity".to_string()));
        }
        let digest = item
            .get("secret_hash")
            .and_then(Value::as_str)
            .ok_or_else(|| ConfigError("invalid projection digest".to_string()))?;
        validate_digest(digest)?;
        let secret_version = item
            .get("secret_version")
            .and_then(Value::as_u64)
            .filter(|version| *version >= 1)
            .ok_or_else(|| ConfigError("invalid secret version".to_string()))?;
        let enabled = item
            .get("enabled")
            .and_then(Value::as_bool)
            .ok_or_else(|| ConfigError("invalid enabled state".to_string()))?;
        let max_concurrency = match item.get("max_concurrency") {
            None | Some(Value::Null) => PROJECTION_DEFAULT_MAX_CONCURRENCY,
            Some(value) => parse_projection_limit(
                value,
                "max_concurrency",
                PROJECTION_MAX_CONCURRENCY_MIN,
                PROJECTION_MAX_CONCURRENCY_MAX,
                PROJECTION_LEGACY_MAX_CONCURRENCY_MAX,
            )?,
        };
        entries.push(ShareEntry {
            key_id: key_id.to_string(),
            name: name.to_string(),
            secret_hash: digest.to_string(),
            secret_version: secret_version as u32,
            enabled,
            max_concurrency,
        });
    }
    Ok(ShareConfig {
        schema_version: schema as u32,
        config_generation: generation,
        share_max_concurrency: share_max,
        legacy: false,
        entries,
    })
}

fn parse_projection_limit(
    value: &Value,
    field: &str,
    minimum: u32,
    maximum: u32,
    legacy_maximum: u32,
) -> Result<u32, ConfigError> {
    let number = value
        .as_u64()
        .filter(|number| *number >= minimum as u64 && *number <= legacy_maximum as u64)
        .ok_or_else(|| {
            ConfigError(format!(
                "projection field {field} must be an integer in {minimum}..{legacy_maximum}"
            ))
        })?;
    Ok((number as u32).min(maximum))
}

fn validate_digest(value: &str) -> Result<(), ConfigError> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(ConfigError("Invalid digest".to_string()));
    }
    Ok(())
}

/// Read and parse the share configuration file.
pub fn load_share_config(
    path: &std::path::Path,
    auth_mode: AuthMode,
) -> Result<ShareConfig, ConfigError> {
    let document = fs::read_to_string(path).map_err(|error| {
        ConfigError(format!(
            "cannot read share config {}: {error}",
            path.display()
        ))
    })?;
    parse_share_config(&document, auth_mode)
}
