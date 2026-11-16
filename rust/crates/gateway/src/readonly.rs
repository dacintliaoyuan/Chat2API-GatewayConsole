//! Read-only 8003 surface: health, readiness, version and model catalog.
//!
//! The shapes follow `chat2api/chat2api.py` (`health_snapshot`,
//! `readiness_snapshot`, the `/v1/version` handler and `list_models`). Missing
//! observations stay `null` and are never folded into 0/false.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::config::{parse_upstream_base_url, upstream_uses_tls, ConfigError, GatewayConfig};
use crate::error::GatewayError;
use crate::login::CredentialSink;
use crate::tls::{self, UpstreamStream};

pub const CHAT2API_VERSION: &str = "1.0.0";
pub const CHAT2API_SOURCE_REV: &str = "e2d60349";
pub const DEFAULT_BUILD_ID: &str = "e2d60349+rust-s1a";
pub const MODELS_CACHE_TTL: Duration = Duration::from_secs(60);
pub const REPETITION_GUARD_AUTO_RETRIES: u32 = 5;
pub const UPSTREAM_AUTO_RETRIES: u32 = 5;
pub const UPSTREAM_SLOW_AUTO_RETRIES: u32 = 2;
pub const UPSTREAM_SLOW_RETRY_THRESHOLD_S: f64 = 30.0;
pub const MAX_TOTAL_ATTEMPTS: u32 = 12;
pub const MAX_REQUEST_SECONDS: u32 = 540;
pub const MAX_IMAGES: u32 = 16;
pub const AUTO_TRIM_HISTORICAL_IMAGES: bool = true;
pub const AUTO_TRIM_CURRENT_TURN_IMAGES: bool = false;
pub const REAUTH_MAX_SESSIONS_PER_CYCLE: u32 = 6;

/// One upstream HTTP observation.
#[derive(Debug)]
pub struct UpstreamResponse {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
}

/// Why an upstream request could not produce an HTTP response.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpstreamFailure {
    Timeout,
    ConnectionError,
}

impl UpstreamFailure {
    pub fn name(self) -> &'static str {
        match self {
            Self::Timeout => "Timeout",
            Self::ConnectionError => "ConnectionError",
        }
    }
}

/// A minimal HTTP/1.1 client for the loopback upstream.
pub struct UpstreamClient {
    host: String,
    port: u16,
    timeout: Duration,
    tls: bool,
}

impl UpstreamClient {
    pub fn from_config(config: &GatewayConfig) -> Result<Self, ConfigError> {
        let (host, port) = parse_upstream_base_url(&config.upstream_base_url)?;
        Ok(Self {
            host,
            port,
            timeout: config.upstream_timeout,
            tls: upstream_uses_tls(&config.upstream_base_url),
        })
    }

    pub fn get(
        &self,
        path: &str,
        bearer: Option<&str>,
    ) -> Result<UpstreamResponse, UpstreamFailure> {
        let addresses: Vec<std::net::SocketAddr> = format!("{}:{}", self.host, self.port)
            .to_socket_addrs()
            .map_err(|_| UpstreamFailure::ConnectionError)?
            .collect();
        if addresses.is_empty() {
            return Err(UpstreamFailure::ConnectionError);
        }
        let mut last = UpstreamFailure::ConnectionError;
        let mut connected = None;
        for address in addresses {
            match TcpStream::connect_timeout(&address, self.timeout) {
                Ok(stream) => {
                    connected = Some(stream);
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::TimedOut => {
                    last = UpstreamFailure::Timeout
                }
                Err(_) => {}
            }
        }
        let stream = connected.ok_or(last)?;
        let _ = stream.set_read_timeout(Some(self.timeout));
        let _ = stream.set_write_timeout(Some(self.timeout));
        let mut stream: UpstreamStream = if self.tls {
            Box::new(tls::wrap(stream, &self.host).map_err(|_| UpstreamFailure::ConnectionError)?)
        } else {
            Box::new(stream)
        };
        let mut request = format!(
            "GET {path} HTTP/1.1\r\nHost: {}:{}\r\nAccept: application/json\r\n\
             Connection: close\r\n",
            self.host, self.port
        );
        if let Some(bearer) = bearer {
            request.push_str(&format!("Authorization: Bearer {bearer}\r\n"));
        }
        request.push_str("\r\n");
        stream
            .write_all(request.as_bytes())
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        stream
            .flush()
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        read_upstream_response(&mut stream)
    }

    pub fn get_json(
        &self,
        path: &str,
        bearer: Option<&str>,
    ) -> Result<(u16, Option<Value>), UpstreamFailure> {
        let response = self.get(path, bearer)?;
        let payload = serde_json::from_slice(&response.body).ok();
        Ok((response.status, payload))
    }
}

fn read_upstream_response(stream: &mut dyn Read) -> Result<UpstreamResponse, UpstreamFailure> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    loop {
        let read = stream
            .read(&mut buffer)
            .map_err(|error| match error.kind() {
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => {
                    UpstreamFailure::Timeout
                }
                _ => UpstreamFailure::ConnectionError,
            })?;
        if read == 0 {
            break;
        }
        raw.extend_from_slice(&buffer[..read]);
        if raw.len() > 64 * 1024 * 1024 {
            return Err(UpstreamFailure::ConnectionError);
        }
    }
    parse_upstream_response(&raw)
}

fn parse_upstream_response(raw: &[u8]) -> Result<UpstreamResponse, UpstreamFailure> {
    let header_end = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or(UpstreamFailure::ConnectionError)?;
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or(UpstreamFailure::ConnectionError)?;
    let mut headers = HashMap::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
        }
    }
    let mut body = raw[header_end + 4..].to_vec();
    if headers
        .get("transfer-encoding")
        .is_some_and(|value| value.to_ascii_lowercase().contains("chunked"))
    {
        body = decode_chunked(&body)?;
    }
    Ok(UpstreamResponse {
        status,
        headers,
        body,
    })
}

fn decode_chunked(raw: &[u8]) -> Result<Vec<u8>, UpstreamFailure> {
    let mut output = Vec::new();
    let mut index = 0;
    loop {
        let line_end = raw[index..]
            .windows(2)
            .position(|window| window == b"\r\n")
            .ok_or(UpstreamFailure::ConnectionError)?
            + index;
        let size_line = std::str::from_utf8(&raw[index..line_end])
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        let size = usize::from_str_radix(size_line.split(';').next().unwrap_or("0").trim(), 16)
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        index = line_end + 2;
        if size == 0 {
            return Ok(output);
        }
        if index + size + 2 > raw.len() {
            return Err(UpstreamFailure::ConnectionError);
        }
        output.extend_from_slice(&raw[index..index + size]);
        index += size + 2;
    }
}

#[derive(Debug, Clone, Default)]
struct ConfigFreshness {
    checked_epoch: Option<f64>,
    attempt_epoch: Option<f64>,
    refreshing: bool,
    last_failure: Option<Value>,
}

#[derive(Debug, Clone)]
struct UpstreamState {
    state: String,
    reason: Option<String>,
    upstream_status: Option<u16>,
    checked_epoch: Option<f64>,
    changed_epoch: f64,
    outage_started_epoch: Option<f64>,
    last_ok_epoch: Option<f64>,
}

/// The read-only proxy state shared by every request.
pub struct ProxyState {
    pub base_url: String,
    bearer: Arc<Mutex<String>>,
    pub build_id: String,
    upstream_config: Mutex<Option<Value>>,
    config_freshness: Mutex<ConfigFreshness>,
    upstream_state: Mutex<UpstreamState>,
    models_cache: Mutex<Option<(Instant, Value)>>,
    upstream: UpstreamClient,
}

impl ProxyState {
    pub fn new(config: &GatewayConfig) -> Result<Self, ConfigError> {
        Self::new_with_bearer(config, Arc::new(Mutex::new(config.upstream_token.clone())))
    }

    pub fn new_with_bearer(
        config: &GatewayConfig,
        bearer: Arc<Mutex<String>>,
    ) -> Result<Self, ConfigError> {
        let build_id = std::env::var("CHAT2API_BUILD")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_BUILD_ID.to_string());
        Ok(Self {
            base_url: config.upstream_base_url.clone(),
            bearer,
            build_id,
            upstream_config: Mutex::new(None),
            config_freshness: Mutex::new(ConfigFreshness::default()),
            upstream_state: Mutex::new(UpstreamState {
                state: "unknown".to_string(),
                reason: Some("not_probed".to_string()),
                upstream_status: None,
                checked_epoch: None,
                changed_epoch: epoch_now(),
                outage_started_epoch: None,
                last_ok_epoch: None,
            }),
            models_cache: Mutex::new(None),
            upstream: UpstreamClient::from_config(config)?,
        })
    }

    /// One public `/api/config` observation, without the user's bearer.
    pub fn probe_config(&self) {
        let observed = self.upstream.get_json("/api/config", None);
        let (config, failure, status) = match observed {
            Ok((status, payload)) => {
                if status == 200 {
                    match payload {
                        Some(payload) if payload.is_object() => (Some(payload), None, Some(status)),
                        _ => (None, Some("invalid_config_shape"), Some(status)),
                    }
                } else if status == 401 || status == 403 {
                    (None, Some("upstream_auth_rejected"), Some(status))
                } else if (500..=599).contains(&status) {
                    (None, Some("upstream_5xx"), Some(status))
                } else {
                    (None, Some("unexpected_status"), Some(status))
                }
            }
            Err(UpstreamFailure::Timeout) => (None, Some("timeout"), None),
            Err(UpstreamFailure::ConnectionError) => (None, Some("network_error"), None),
        };
        let mut freshness = self.config_freshness.lock().expect("config poisoned");
        freshness.attempt_epoch = Some(epoch_now());
        if let Some(config) = config {
            *self.upstream_config.lock().expect("config poisoned") = Some(config);
            freshness.checked_epoch = Some(epoch_now());
            freshness.last_failure = None;
            self.record_reachability("ok", Some("config_probe_ok"), status);
        } else {
            freshness.last_failure = Some(json!({
                "reason": failure,
                "at_epoch": epoch_now(),
                "upstream_status": status,
            }));
            self.record_reachability("unknown", Some("config_probe_failed"), status);
        }
    }

    fn config_summary(&self) -> Value {
        let config = self
            .upstream_config
            .lock()
            .expect("config poisoned")
            .clone()
            .unwrap_or(Value::Null);
        let freshness = self
            .config_freshness
            .lock()
            .expect("config poisoned")
            .clone();
        let features = config.get("features");
        let oauth = config.get("oauth");
        let providers = oauth.and_then(|oauth| oauth.get("providers"));
        let oauth_providers: Vec<Value> = providers
            .and_then(Value::as_object)
            .map(|providers| {
                let mut keys: Vec<&String> = providers.keys().collect();
                keys.sort();
                keys.into_iter().cloned().map(Value::String).collect()
            })
            .unwrap_or_default();
        json!({
            "name": config.get("name").cloned().unwrap_or(Value::Null),
            "version": config.get("version").cloned().unwrap_or(Value::Null),
            "enable_api_keys": features
                .and_then(|features| features.get("enable_api_keys"))
                .cloned()
                .unwrap_or(Value::Null),
            "oauth_providers": oauth_providers,
            "config_checked_epoch": freshness.checked_epoch,
            "config_attempt_epoch": freshness.attempt_epoch,
            "config_refreshing": freshness.refreshing,
            "config_last_failure": freshness.last_failure,
        })
    }

    fn auth_state(&self) -> &'static str {
        if self.bearer().is_empty() {
            "needs_login"
        } else {
            "valid"
        }
    }

    fn auth_label(&self) -> &'static str {
        match self.auth_state() {
            "valid" => "认证有效",
            _ => "需要登录",
        }
    }

    fn record_reachability(&self, state: &str, reason: Option<&str>, upstream_status: Option<u16>) {
        let now = epoch_now();
        let mut upstream = self.upstream_state.lock().expect("upstream state poisoned");
        let previous = upstream.state.clone();
        let had_observation = upstream.checked_epoch.is_some();
        let changed = previous != state;
        let outage_started = if state == "unreachable" {
            upstream.outage_started_epoch.or(Some(now))
        } else {
            upstream.outage_started_epoch
        };
        let last_ok = if state == "ok" {
            Some(now)
        } else {
            upstream.last_ok_epoch
        };
        let changed_epoch = if changed { now } else { upstream.changed_epoch };
        *upstream = UpstreamState {
            state: state.to_string(),
            reason: reason.map(str::to_string),
            upstream_status,
            checked_epoch: Some(now),
            changed_epoch,
            outage_started_epoch: if state == "ok" { None } else { outage_started },
            last_ok_epoch: last_ok,
        };
        let _ = had_observation;
    }

    fn upstream_snapshot(&self) -> Value {
        let upstream = self.upstream_state.lock().expect("upstream state poisoned");
        json!({
            "state": upstream.state,
            "reason": upstream.reason,
            "upstream_status": upstream.upstream_status,
            "checked_epoch": upstream.checked_epoch,
            "changed_epoch": upstream.changed_epoch,
            "outage_started_epoch": upstream.outage_started_epoch,
            "last_ok_epoch": upstream.last_ok_epoch,
        })
    }

    pub fn health_snapshot(&self) -> Value {
        let models = self
            .models_cache
            .lock()
            .expect("models cache poisoned")
            .as_ref()
            .and_then(|(_, payload)| payload.get("data"))
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        json!({
            "status": "ok",
            "chat2api_version": CHAT2API_VERSION,
            "chat2api_build": self.build_id,
            "backend": self.base_url,
            "models": models,
            "authentication": {
                "state": self.auth_state(),
                "label": self.auth_label(),
                "error": Value::Null,
                "primary": Value::Null,
            },
            "watch": {
                "thread_alive": false,
                "last_round_epoch": Value::Null,
                "last_round_outcome": Value::Null,
                "next_round_due": Value::Null,
                "rounds": 0,
            },
            "recovery": {
                "state": "idle",
                "session_starts": 0,
                "max_sessions": REAUTH_MAX_SESSIONS_PER_CYCLE,
                "next_attempt_epoch": Value::Null,
            },
            "login_window_deadline_epoch": Value::Null,
            "login_window_s": Value::Null,
            "upstream": self.config_summary(),
            "upstream_reachability": self.upstream_snapshot(),
            "docs": "POST /v1/chat/completions",
        })
    }

    /// Side-effect-free readiness: the current bearer must fetch a catalog.
    pub fn readiness_snapshot(&self) -> (bool, Value) {
        let (verdict, reason, upstream_status) = self.classify_credential();
        let reachability = match reason {
            Some("timeout") | Some("network_error") => "unreachable",
            _ if verdict == "needs_login" => "unknown",
            _ if upstream_status.is_some() => "ok",
            _ => "unknown",
        };
        self.record_reachability(reachability, reason, upstream_status);
        let ready = verdict == "valid";
        (
            ready,
            json!({
                "status": if ready { "ready" } else { "not_ready" },
                "authentication": verdict,
                "reason": reason,
                "upstream_status": upstream_status,
                "upstream": self.config_summary(),
                "upstream_reachability": self.upstream_snapshot(),
            }),
        )
    }

    fn classify_credential(&self) -> (&'static str, Option<&'static str>, Option<u16>) {
        let bearer = self.bearer();
        if bearer.is_empty() {
            return ("needs_login", Some("no_local_credential"), None);
        }
        match self.upstream.get_json("/api/models", Some(&bearer)) {
            Err(UpstreamFailure::Timeout) => ("unknown", Some("timeout"), None),
            Err(UpstreamFailure::ConnectionError) => ("unknown", Some("network_error"), None),
            Ok((status, payload)) => {
                if status == 401 || status == 403 {
                    ("rejected", Some("upstream_rejected"), Some(status))
                } else if (500..=599).contains(&status) {
                    ("unknown", Some("upstream_5xx"), Some(status))
                } else if status != 200 {
                    ("unknown", Some("unexpected_status"), Some(status))
                } else if payload
                    .as_ref()
                    .and_then(Value::as_object)
                    .and_then(|payload| payload.get("data"))
                    .and_then(Value::as_array)
                    .is_none()
                {
                    ("unknown", Some("invalid_catalog_shape"), Some(status))
                } else {
                    ("valid", None, Some(status))
                }
            }
        }
    }

    pub fn version(&self) -> Result<Value, GatewayError> {
        match self.upstream.get_json("/api/version", None) {
            Err(failure) => Err(GatewayError::new(
                502,
                "Failed to fetch upstream version",
                failure.name(),
            )),
            Ok((status, payload)) => {
                if status != 200 {
                    return Ok(json!({
                        "openwebui": Value::Null,
                        "chat2api": CHAT2API_VERSION,
                        "chat2api_build": self.build_id,
                        "chat2api_source_rev": CHAT2API_SOURCE_REV,
                        "retry_policy": retry_policy(),
                    }));
                }
                match payload {
                    Some(payload) => Ok(json!({
                        "openwebui": payload,
                        "chat2api": CHAT2API_VERSION,
                        "chat2api_build": self.build_id,
                        "chat2api_source_rev": CHAT2API_SOURCE_REV,
                        "retry_policy": retry_policy(),
                    })),
                    None => Err(GatewayError::new(
                        502,
                        "Upstream version response was not valid JSON",
                        "ValueError",
                    )),
                }
            }
        }
    }

    /// The model catalog; `raw_fresh` is the share entry's strict refresh.
    pub fn models(&self, raw_fresh: bool) -> Result<(Value, bool), GatewayError> {
        let bearer = self.bearer();
        let observed = self.upstream.get_json("/api/models", Some(&bearer));
        let fetched = match observed {
            Ok((200, payload)) => payload.filter(|payload| {
                payload.is_object() && payload.get("data").and_then(Value::as_array).is_some()
            }),
            Ok((401 | 403, _)) => {
                return Err(GatewayError::reauthentication_required(Some(
                    "upstream_rejected",
                )));
            }
            _ => None,
        };
        if let Some(payload) = fetched {
            *self.models_cache.lock().expect("models cache poisoned") =
                Some((Instant::now(), payload.clone()));
            return Ok((payload, raw_fresh));
        }
        if raw_fresh {
            return Err(GatewayError::models_unavailable());
        }
        let cached = self
            .models_cache
            .lock()
            .expect("models cache poisoned")
            .clone();
        match cached {
            Some((_, payload)) => Ok((payload, false)),
            None => Err(GatewayError::models_unavailable()),
        }
    }

    /// Whether the current bearer is accepted right now.
    pub fn credential_valid(&self) -> bool {
        self.classify_credential().0 == "valid"
    }

    /// The current in-memory bearer.
    pub fn bearer(&self) -> String {
        self.bearer.lock().expect("bearer poisoned").clone()
    }

    /// Publish a newly committed credential into the running process.
    pub fn set_bearer(&self, token: &str) {
        *self.bearer.lock().expect("bearer poisoned") = token.to_string();
    }

    /// Drop the cached catalog after a credential change.
    pub fn invalidate_models(&self) {
        *self.models_cache.lock().expect("models cache poisoned") = None;
    }

    /// The `/admin/auth/verify` single-shot observation.
    pub fn verify_snapshot(&self) -> Value {
        let (verdict, reason, upstream_status) = self.classify_credential();
        json!({
            "verify": verdict,
            "reason": reason,
            "upstream_status": upstream_status,
            "checked_epoch": epoch_now() as u64,
            "note": "single-shot upstream /api/models probe; auth state and model cache untouched",
        })
    }
}

impl CredentialSink for ProxyState {
    fn publish_credential(&self, token: &str, _api_key: Option<&str>) {
        self.set_bearer(token);
        self.invalidate_models();
    }
}

fn retry_policy() -> Value {
    json!({
        "repetition_retries": REPETITION_GUARD_AUTO_RETRIES,
        "upstream_fast_retries": UPSTREAM_AUTO_RETRIES,
        "upstream_slow_retries": UPSTREAM_SLOW_AUTO_RETRIES,
        "slow_threshold_seconds": UPSTREAM_SLOW_RETRY_THRESHOLD_S,
        "max_physical_attempts": MAX_TOTAL_ATTEMPTS,
        "max_request_seconds": MAX_REQUEST_SECONDS,
        "max_images": MAX_IMAGES,
        "auto_trim_historical_images": AUTO_TRIM_HISTORICAL_IMAGES,
        "auto_trim_current_turn_images": AUTO_TRIM_CURRENT_TURN_IMAGES,
    })
}

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}
