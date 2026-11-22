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
/// The cloud process keeps Python's 180 second default (the share gateway).
pub const DEFAULT_CLOUD_AUTH_WAIT_BUDGET_SECONDS: f64 = 180.0;
pub const MIN_AUTH_WAIT_BUDGET_SECONDS: f64 = 1.0;
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
/// The explicit login round's total browser budget (Python `--login-timeout`).
pub const DEFAULT_LOGIN_TIMEOUT_SECONDS: u64 = 1800;
pub const MIN_LOGIN_TIMEOUT_SECONDS: u64 = 1;
pub const MAX_LOGIN_TIMEOUT_SECONDS: u64 = 86_400;
/// A login session with no observable progress is closed after this.
pub const DEFAULT_LOGIN_IDLE_TIMEOUT_SECONDS: u64 = 300;
pub const DEFAULT_LOGIN_POLL_INTERVAL_MS: u64 = 1000;
pub const DEFAULT_LOGIN_CLICK_OBSERVE_TIMEOUT_MS: u64 = 8000;
pub const DEFAULT_LOGIN_STAGE_SETTLE_TIMEOUT_SECONDS: u64 = 20;
pub const DEFAULT_LOGIN_MAX_CLICKS_PER_STAGE: usize = 2;
pub const DEFAULT_LOGIN_NAVIGATION_ATTEMPTS: usize = 2;
/// The Python recovery cycle's bounded session budget.
pub const DEFAULT_LOGIN_MAX_SESSIONS: usize = 6;
/// The bounded automatic re-open interval after a retryable exhausted round.
pub const DEFAULT_RECOVERY_RETRY_INTERVAL_SECONDS: u64 = 1800;
pub const MIN_RECOVERY_RETRY_INTERVAL_SECONDS: u64 = 60;
pub const MAX_RECOVERY_RETRY_INTERVAL_SECONDS: u64 = 86_400;
/// Automatic re-open rounds allowed inside one rolling day.
pub const DEFAULT_RECOVERY_RETRY_MAX_PER_DAY: usize = 3;
pub const MIN_RECOVERY_RETRY_MAX_PER_DAY: usize = 0;
pub const MAX_RECOVERY_RETRY_MAX_PER_DAY: usize = 24;
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
/// tests. Production must be selected explicitly and only binds 8000/8002/8003 on
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

/// The process shape selected by `--mode`.
///
/// `Gateway` is the existing single process. `Worker` is the local model worker:
/// it is a normal gateway that binds `127.0.0.1` and writes its actual port to a
/// port file. `Ingress` is the local 8000 listener that forwards to the worker;
/// it does not assemble a gateway server of its own.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum RunMode {
    #[default]
    Gateway,
    Worker,
    Ingress,
}

impl RunMode {
    pub fn label(self) -> &'static str {
        match self {
            Self::Gateway => "gateway",
            Self::Worker => "worker",
            Self::Ingress => "ingress",
        }
    }
}

/// One gateway role.
///
/// The local role assembles the general `/api/*` passthrough for the local 8000
/// process; the cloud role keeps that surface unassembled on 8002/8003.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Role {
    Local,
    #[default]
    Cloud,
}

impl Role {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "local" => Some(Self::Local),
            "cloud" => Some(Self::Cloud),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Cloud => "cloud",
        }
    }

    /// Derive the role from the production port when the CLI does not name it:
    /// 7998/7999/8000 are the local process (desktop test/default), 8002/8003
    /// are the cloud pair, and the candidate's ephemeral port stays cloud.
    pub fn for_port(port: u16) -> Self {
        if matches!(port, 7998 | 7999 | 8000) {
            Self::Local
        } else {
            Self::Cloud
        }
    }

    /// Python `resolve_auth_wait_budget`: the local 8000 process defaults to
    /// 30 seconds and the cloud 8002/8003 process keeps 180 seconds.
    pub fn default_auth_wait_budget(self) -> Duration {
        match self {
            Self::Local => Duration::from_secs_f64(DEFAULT_AUTH_WAIT_BUDGET_SECONDS),
            Self::Cloud => Duration::from_secs_f64(DEFAULT_CLOUD_AUTH_WAIT_BUDGET_SECONDS),
        }
    }
}

/// One gateway configuration.
#[derive(Debug, Clone)]
pub struct GatewayConfig {
    pub bind_host: String,
    pub bind_port: u16,
    /// The role selects the local-only API surface; it is derived from the
    /// production port when the CLI does not name it.
    pub role: Role,
    pub upstream_base_url: String,
    pub upstream_token: String,
    pub share_config: Option<PathBuf>,
    pub auth_mode: AuthMode,
    pub allowed_origins: BTreeSet<String>,
    pub control_token_file: Option<PathBuf>,
    pub local_api_key: String,
    /// Base models that get `-Fast/-Low/-Medium/-High` virtual variants in the
    /// model catalog. Python `--effort-model` is repeatable and empty by default.
    pub effort_models: Vec<String>,
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
    /// Durable UM upstream fault record path (#103). `None` keeps a candidate
    /// in memory only; the local production role defaults to
    /// `data/upstream-faults.json`.
    pub upstream_fault_log: Option<PathBuf>,
    /// Request-body read budget; a stalled body gets 408 after this.
    pub body_read_timeout: Duration,
    /// Total wall-clock budget of one Chat request.
    pub request_total: Duration,
    /// Cumulative authentication wait of one logical request (#63). `None`
    /// selects the role default: 30 seconds local, 180 seconds cloud.
    pub auth_wait_budget: Option<Duration>,
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
    /// Test seam: the base auth-watch interval (the first wait and the valid
    /// reset). `None` uses the production `AUTH_WATCH_INTERVAL`
    /// (Python `AUTH_WATCH_INTERVAL_S`).
    pub auth_watch_interval: Option<Duration>,
    /// Python-compatible credential file; `None` uses the role default.
    pub token_file: Option<PathBuf>,
    /// Durable auth evidence file (#114). `None` uses the role default in
    /// production and stays memory-only in candidate mode.
    pub auth_state_file: Option<PathBuf>,
    /// Durable login-round history file (#115). `None` uses the local
    /// production default and stays memory-only in candidate mode.
    pub login_history_file: Option<PathBuf>,
    /// The browser profile directory; `None` uses the role default.
    pub profile_dir: Option<PathBuf>,
    /// The on-demand manual display marker file.
    pub login_display_file: Option<PathBuf>,
    /// Total budget of one login browser session.
    pub login_timeout: Duration,
    /// A session with no observable progress is closed after this.
    pub login_idle_timeout: Duration,
    /// How often the login page state machine polls.
    pub login_poll_interval: Duration,
    /// How long one click is observed before a retry is allowed.
    pub login_click_observe_timeout: Duration,
    /// How long a page gets to settle before it is treated as changed.
    pub login_stage_settle_timeout: Duration,
    /// Each login stage may be clicked once plus one evidence-backed retry.
    pub login_max_clicks_per_stage: usize,
    /// A failed first navigation is retried once before the page is unknown.
    pub login_navigation_attempts: usize,
    /// Bounded login sessions per recovery round.
    pub login_max_sessions: usize,
    /// The interval before a retryable exhausted round re-opens automatically.
    pub recovery_retry_interval: Duration,
    /// Automatic re-open rounds allowed inside one rolling day.
    pub recovery_retry_max_per_day: usize,
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
    /// The process shape selected by `--mode`; gateway by default.
    pub run_mode: RunMode,
    /// A separately supervised, loopback-only desktop instance.
    pub desktop_instance: bool,
    /// The broker instance key file; `None` disables the instance channel.
    pub instance_key_file: Option<PathBuf>,
    /// The instance-channel AF_UNIX socket path prefix.
    pub instance_pipe_prefix: Option<String>,
    /// The broker-readable instance registration file.
    pub registration_file: Option<PathBuf>,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        Self {
            bind_host: "127.0.0.1".to_string(),
            bind_port: 0,
            role: Role::Cloud,
            upstream_base_url: String::new(),
            upstream_token: String::new(),
            share_config: None,
            auth_mode: AuthMode::Auto,
            allowed_origins: BTreeSet::new(),
            control_token_file: None,
            local_api_key: String::new(),
            effort_models: Vec::new(),
            share_max_request_bytes: DEFAULT_SHARE_MAX_REQUEST_BYTES,
            max_request_bytes: DEFAULT_SHARE_MAX_REQUEST_BYTES,
            local_max_request_bytes: CONSOLE_MAX_REQUEST_BYTES,
            upstream_timeout: DEFAULT_UPSTREAM_TIMEOUT,
            nonstream_upstream_aggregate: false,
            nonstream_aggregate_scope: None,
            usage_log: None,
            service_log: None,
            upstream_fault_log: None,
            body_read_timeout: Duration::from_secs(DEFAULT_BODY_READ_TIMEOUT_SECONDS),
            request_total: Duration::from_secs(DEFAULT_REQUEST_TOTAL_SECONDS),
            auth_wait_budget: None,
            auth_wait_max_concurrent: DEFAULT_AUTH_WAIT_MAX_CONCURRENT,
            auth_wait_recheck: Duration::from_secs_f64(DEFAULT_AUTH_WAIT_RECHECK_SECONDS),
            reauth_attempt_window: Duration::from_secs(DEFAULT_REAUTH_ATTEMPT_WINDOW_SECONDS),
            reauth_max_attempts_per_window: DEFAULT_REAUTH_MAX_ATTEMPTS_PER_WINDOW,
            browser_disabled: false,
            auth_watch_interval: None,
            token_file: None,
            auth_state_file: None,
            login_history_file: None,
            profile_dir: None,
            login_display_file: None,
            login_timeout: Duration::from_secs(DEFAULT_LOGIN_TIMEOUT_SECONDS),
            login_idle_timeout: Duration::from_secs(DEFAULT_LOGIN_IDLE_TIMEOUT_SECONDS),
            login_poll_interval: Duration::from_millis(DEFAULT_LOGIN_POLL_INTERVAL_MS),
            login_click_observe_timeout: Duration::from_millis(
                DEFAULT_LOGIN_CLICK_OBSERVE_TIMEOUT_MS,
            ),
            login_stage_settle_timeout: Duration::from_secs(
                DEFAULT_LOGIN_STAGE_SETTLE_TIMEOUT_SECONDS,
            ),
            login_max_clicks_per_stage: DEFAULT_LOGIN_MAX_CLICKS_PER_STAGE,
            login_navigation_attempts: DEFAULT_LOGIN_NAVIGATION_ATTEMPTS,
            login_max_sessions: DEFAULT_LOGIN_MAX_SESSIONS,
            recovery_retry_interval: Duration::from_secs(DEFAULT_RECOVERY_RETRY_INTERVAL_SECONDS),
            recovery_retry_max_per_day: DEFAULT_RECOVERY_RETRY_MAX_PER_DAY,
            first_byte_timeout: DEFAULT_FIRST_BYTE_TIMEOUT,
            socket_silence_timeout: DEFAULT_SOCKET_SILENCE_TIMEOUT,
            max_fast_retries: DEFAULT_MAX_FAST_RETRIES,
            max_slow_retries: DEFAULT_MAX_SLOW_RETRIES,
            max_physical_attempts: DEFAULT_MAX_PHYSICAL_ATTEMPTS,
            retry_delay_base: DEFAULT_RETRY_DELAY_BASE,
            slow_retry_threshold: DEFAULT_SLOW_RETRY_THRESHOLD,
            candidate: true,
            run_mode: RunMode::Gateway,
            desktop_instance: false,
            instance_key_file: None,
            instance_pipe_prefix: None,
            registration_file: None,
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
    [5173, 7998, 7999, 8000, 8001, 8002, 8003, 8097, 8099]
        .into_iter()
        .collect()
}

/// The only ports production mode may bind.
pub fn production_ports() -> BTreeSet<u16> {
    [7998, 7999, 8000, 8002, 8003].into_iter().collect()
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
        if let Ok(value) = std::env::var("CHAT2API_TOKEN_FILE") {
            if !value.trim().is_empty() {
                config.token_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_AUTH_STATE_FILE") {
            if !value.trim().is_empty() {
                config.auth_state_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(crate::login::history::LOGIN_HISTORY_FILE_ENV) {
            if !value.trim().is_empty() {
                config.login_history_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_PROFILE_DIR") {
            if !value.trim().is_empty() {
                config.profile_dir = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var("CHAT2API_LOGIN_DISPLAY_FILE") {
            if !value.trim().is_empty() {
                config.login_display_file = Some(PathBuf::from(value));
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
        if let Ok(value) = std::env::var("CHAT2API_UPSTREAM_FAULT_LOG") {
            if !value.trim().is_empty() {
                config.upstream_fault_log = Some(PathBuf::from(value));
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
        config.login_timeout =
            parse_login_timeout(std::env::var("CHAT2API_LOGIN_TIMEOUT_S").ok().as_deref());
        config.login_idle_timeout = parse_duration_seconds(
            std::env::var("CHAT2API_LOGIN_IDLE_TIMEOUT_S")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_IDLE_TIMEOUT_SECONDS,
        );
        config.login_poll_interval = parse_duration_millis(
            std::env::var("CHAT2API_LOGIN_POLL_INTERVAL_MS")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_POLL_INTERVAL_MS,
        );
        config.login_click_observe_timeout = parse_duration_millis(
            std::env::var("CHAT2API_LOGIN_CLICK_OBSERVE_TIMEOUT_MS")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_CLICK_OBSERVE_TIMEOUT_MS,
        );
        config.login_stage_settle_timeout = parse_duration_seconds(
            std::env::var("CHAT2API_LOGIN_STAGE_SETTLE_TIMEOUT_S")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_STAGE_SETTLE_TIMEOUT_SECONDS,
        );
        config.login_max_clicks_per_stage = parse_usize(
            std::env::var("CHAT2API_LOGIN_MAX_CLICKS_PER_STAGE")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_MAX_CLICKS_PER_STAGE,
            1,
            10,
        );
        config.login_navigation_attempts = parse_usize(
            std::env::var("CHAT2API_LOGIN_NAVIGATION_ATTEMPTS")
                .ok()
                .as_deref(),
            DEFAULT_LOGIN_NAVIGATION_ATTEMPTS,
            1,
            10,
        );
        config.login_max_sessions = parse_usize(
            std::env::var("CHAT2API_LOGIN_MAX_SESSIONS").ok().as_deref(),
            DEFAULT_LOGIN_MAX_SESSIONS,
            1,
            100,
        );
        config.recovery_retry_interval = parse_recovery_retry_interval(
            std::env::var("CHAT2API_RECOVERY_RETRY_INTERVAL_S")
                .ok()
                .as_deref(),
        );
        config.recovery_retry_max_per_day = parse_usize(
            std::env::var("CHAT2API_RECOVERY_RETRY_MAX_PER_DAY")
                .ok()
                .as_deref(),
            DEFAULT_RECOVERY_RETRY_MAX_PER_DAY,
            MIN_RECOVERY_RETRY_MAX_PER_DAY,
            MAX_RECOVERY_RETRY_MAX_PER_DAY,
        );
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

    /// The effective per-request cumulative authentication-wait budget.
    pub fn auth_wait_budget(&self) -> Duration {
        self.auth_wait_budget
            .unwrap_or_else(|| self.role.default_auth_wait_budget())
    }

    /// The durable upstream fault record path (#103).
    ///
    /// The local production process (8000) owns `data/upstream-faults.json` by
    /// default; a candidate test keeps the record in memory unless it injects an
    /// explicit path, and the cloud role never reads the local file.
    pub fn effective_upstream_fault_log(&self) -> Option<PathBuf> {
        crate::upstream_fault::effective_path(
            self.upstream_fault_log.clone(),
            self.role == Role::Local,
            self.candidate,
        )
    }

    /// The durable auth evidence path (#114).
    ///
    /// An explicit setting wins in every mode; without one, production uses the
    /// role default and a candidate never writes a production file.
    pub fn effective_auth_state_file(&self) -> Option<PathBuf> {
        crate::auth_state::effective_path(self.auth_state_file.clone(), self.role, self.candidate)
    }

    /// The durable login-round history path (#115/#116).
    ///
    /// An explicit setting wins in every mode; without one, production uses the
    /// role default (local `data/login-history.jsonl`, cloud
    /// `/var/lib/um-share/login-history.jsonl`) and a candidate keeps the
    /// history in memory.
    pub fn effective_login_history_file(&self) -> Option<PathBuf> {
        crate::login::history::effective_path(
            self.login_history_file.clone(),
            self.role,
            self.candidate,
        )
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
        match self.run_mode {
            RunMode::Worker => {
                // The worker is the local model process. Production passes port 0;
                // a fixed test port must stay outside the governed console/gateway
                // pair so a worker can never take 8000/8001 from the ingress.
                if self.bind_port != 0
                    && (self.bind_port < 1024 || governed_ports().contains(&self.bind_port))
                {
                    return Err(ConfigError(format!(
                        "worker must bind an ephemeral loopback port or a non-governed high port, got {}",
                        self.bind_port
                    )));
                }
            }
            RunMode::Ingress => {
                return Err(ConfigError(
                    "ingress mode is served by the ingress listener, not the gateway server"
                        .to_string(),
                ));
            }
            RunMode::Gateway => {
                if self.desktop_instance
                    && (self.role != Role::Local
                        || self.share_config.is_some()
                        || self.control_token_file.is_none())
                {
                    return Err(ConfigError(
                        "desktop instances require the local role, a control token file, and no share configuration".to_string(),
                    ));
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
                } else if self.desktop_instance {
                    // Keep the owned listener outside the shared repository segments
                    // and their legacy aliases. 7998/7999 are already desktop ports.
                    if matches!(
                        self.bind_port,
                        0 | 5173 | 5901 | 8000..=8009 | 8090..=8119 | 8300..=8399 | 18096
                    ) {
                        return Err(ConfigError(format!(
                            "desktop production port is reserved: {}",
                            self.bind_port
                        )));
                    }
                } else {
                    if !production_ports().contains(&self.bind_port) {
                        return Err(ConfigError(format!(
                            "production must bind one of 7998, 7999, 8000, 8002 or 8003 on loopback, got {}",
                            self.bind_port
                        )));
                    }
                    // #75 user story 17: the local role assembles the general `/api/*`
                    // surface, so it may never run on the cloud 8002/8003 pair; the
                    // cloud role keeps that surface unassembled on 8002/8003 only.
                    let role_allowed = match self.role {
                        Role::Local => matches!(self.bind_port, 7998 | 7999 | 8000),
                        Role::Cloud => matches!(self.bind_port, 8002 | 8003),
                    };
                    if !role_allowed {
                        let allowed = match self.role {
                            Role::Local => "7998, 7999 or 8000",
                            Role::Cloud => "8002 or 8003",
                        };
                        return Err(ConfigError(format!(
                            "production role {} must bind {}, got {}",
                            self.role.as_str(),
                            allowed,
                            self.bind_port
                        )));
                    }
                }
            }
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

/// Parse the login browser budget; invalid values keep the default.
pub fn parse_login_timeout(value: Option<&str>) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(DEFAULT_LOGIN_TIMEOUT_SECONDS);
    Duration::from_secs(seconds.clamp(MIN_LOGIN_TIMEOUT_SECONDS, MAX_LOGIN_TIMEOUT_SECONDS))
}

/// Parse the automatic re-open interval; invalid values keep the default and
/// valid values clamp to 60..86400 seconds.
pub fn parse_recovery_retry_interval(value: Option<&str>) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(DEFAULT_RECOVERY_RETRY_INTERVAL_SECONDS);
    Duration::from_secs(seconds.clamp(
        MIN_RECOVERY_RETRY_INTERVAL_SECONDS,
        MAX_RECOVERY_RETRY_INTERVAL_SECONDS,
    ))
}

fn parse_duration_seconds(value: Option<&str>, fallback: u64) -> Duration {
    let seconds = value
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value > 0.0)
        .unwrap_or(fallback as f64);
    Duration::from_secs_f64(seconds)
}

fn parse_duration_millis(value: Option<&str>, fallback: u64) -> Duration {
    let millis = value
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(fallback);
    Duration::from_millis(millis.max(1))
}

fn parse_usize(value: Option<&str>, fallback: usize, minimum: usize, maximum: usize) -> usize {
    value
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(fallback)
        .clamp(minimum, maximum)
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

/// Parse the per-request cumulative authentication-wait budget. An absent or
/// invalid value keeps the role default; an explicit value is clamped to 1..180,
/// exactly like Python's `_clamp_auth_wait_budget`.
pub fn parse_auth_wait_budget(value: Option<&str>) -> Option<Duration> {
    let seconds = value?
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite())?;
    Some(Duration::from_secs_f64(seconds.clamp(
        MIN_AUTH_WAIT_BUDGET_SECONDS,
        MAX_AUTH_WAIT_BUDGET_SECONDS,
    )))
}

/// Resolve the auth-wait budget with Python's precedence: an explicit CLI value
/// wins, then the environment, then the role default. Explicit values clamp to
/// 1..180 so no operator value can disable the budget or exceed the ceiling.
pub fn resolve_auth_wait_budget(
    cli_seconds: Option<f64>,
    env_value: Option<&str>,
    role: Role,
) -> Duration {
    if let Some(seconds) = cli_seconds.filter(|value| value.is_finite()) {
        return Duration::from_secs_f64(
            seconds.clamp(MIN_AUTH_WAIT_BUDGET_SECONDS, MAX_AUTH_WAIT_BUDGET_SECONDS),
        );
    }
    parse_auth_wait_budget(env_value).unwrap_or_else(|| role.default_auth_wait_budget())
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

/// Parse and validate the upstream base URL. Both plain HTTP and HTTPS are
/// supported: the production upstream is HTTPS and the gateway terminates TLS
/// itself. The returned host/port are the TCP destination.
pub fn parse_upstream_base_url(value: &str) -> Result<(String, u16), ConfigError> {
    let rest = value
        .strip_prefix("http://")
        .or_else(|| value.strip_prefix("https://"))
        .ok_or_else(|| {
            ConfigError(format!(
                "upstream base URL must be http:// or https://: {value}"
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
        None => {
            let default_port = if value.starts_with("https://") {
                443
            } else {
                80
            };
            (rest, default_port)
        }
    };
    if host.is_empty() || host.contains('@') {
        return Err(ConfigError(format!(
            "upstream base URL must not carry credentials: {value}"
        )));
    }
    Ok((host.to_string(), port))
}

/// Whether the configured upstream requires TLS.
pub fn upstream_uses_tls(value: &str) -> bool {
    value.starts_with("https://")
}

/// Build the HTTP `Host` header for the configured upstream.
///
/// The default scheme port must be omitted: the production upstream answers
/// `Host: chat.genai.um.edu.mo:443` with 503 while `Host: chat.genai.um.edu.mo`
/// succeeds. A non-default port still has to be sent so the upstream can route
/// the request to the right virtual host.
pub fn upstream_host_header(value: &str) -> Result<String, ConfigError> {
    let (host, port) = parse_upstream_base_url(value)?;
    let default_port = if upstream_uses_tls(value) { 443 } else { 80 };
    if port == default_port {
        Ok(host)
    } else {
        Ok(format!("{host}:{port}"))
    }
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
