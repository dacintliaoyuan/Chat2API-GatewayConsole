//! Read-only 8003 surface: health, readiness, version and model catalog.
//!
//! The shapes follow `chat2api/chat2api.py` (`health_snapshot`,
//! `readiness_snapshot`, the `/v1/version` handler and `list_models`). Missing
//! observations stay `null` and are never folded into 0/false.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::config::{
    parse_upstream_base_url, upstream_host_header, upstream_uses_tls, ConfigError, GatewayConfig,
};
use crate::error::GatewayError;
use crate::http::Response;
use crate::login::{CredentialEvidence, CredentialSink};
use crate::tls::{self, UpstreamStream};
use crate::upstream_fault::{
    fault_class_of_status, summarize_error_text, FaultClass, FaultObservation, FaultSource,
    UpstreamFaultLog,
};

pub const CHAT2API_VERSION: &str = "1.0.0";
pub const CHAT2API_SOURCE_REV: &str = "e2d60349";
pub const DEFAULT_BUILD_ID: &str = "e2d60349+rust-s1a";
/// Python `AUTH_WATCH_PROBE_TIMEOUT_S`: the readiness/auth probe budget.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(20);
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
/// Python `EFFORT_LEVELS`: suffix -> OpenAI `reasoning_effort`.
pub const EFFORT_LEVELS: [(&str, &str); 4] = [
    ("Fast", "none"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
];
/// Python `Proxy.call`'s non-stream socket budget for a local `/api/*`
/// passthrough.
pub const PASSTHROUGH_TIMEOUT: Duration = Duration::from_secs(600);

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

/// A browser-like User-Agent: the public Cloudflare edge refuses requests
/// without a browser signature (error 1010); the loopback upstream ignores the
/// header. It is a request header, not a credential.
pub const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36";

/// A minimal HTTP/1.1 client for the loopback upstream.
pub struct UpstreamClient {
    host: String,
    host_header: String,
    port: u16,
    timeout: Duration,
    tls: bool,
    /// Only the public Key probe sets a browser UA; the gateway's own upstream
    /// calls keep no User-Agent.
    user_agent: Option<String>,
}

impl UpstreamClient {
    pub fn from_config(config: &GatewayConfig) -> Result<Self, ConfigError> {
        let (host, port) = parse_upstream_base_url(&config.upstream_base_url)?;
        Ok(Self {
            host_header: upstream_host_header(&config.upstream_base_url)?,
            host,
            port,
            timeout: config.upstream_timeout,
            tls: upstream_uses_tls(&config.upstream_base_url),
            user_agent: None,
        })
    }

    /// Attach the browser-like User-Agent used only by the public Key probe.
    pub fn with_user_agent(mut self, user_agent: &str) -> Self {
        self.user_agent = Some(user_agent.to_string());
        self
    }

    pub fn get(
        &self,
        path: &str,
        bearer: Option<&str>,
    ) -> Result<UpstreamResponse, UpstreamFailure> {
        self.request("GET", path, bearer, None, self.timeout)
    }

    /// One upstream request for the local `/api/*` passthrough.
    ///
    /// Python `Proxy.call` sends the stored bearer, the JSON body and the
    /// default 600 second non-stream socket budget; the client's own credential
    /// never leaves this process.
    pub fn request(
        &self,
        method: &str,
        path: &str,
        bearer: Option<&str>,
        body: Option<&[u8]>,
        timeout: Duration,
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
            match TcpStream::connect_timeout(&address, timeout) {
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
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        let mut stream: UpstreamStream = if self.tls {
            Box::new(tls::wrap(stream, &self.host).map_err(|_| UpstreamFailure::ConnectionError)?)
        } else {
            Box::new(stream)
        };
        let mut request = format!(
            "{method} {path} HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\n\
             Connection: close\r\n",
            self.host_header
        );
        if let Some(user_agent) = &self.user_agent {
            request.push_str(&format!("User-Agent: {user_agent}\r\n"));
        }
        if let Some(bearer) = bearer {
            request.push_str(&format!("Authorization: Bearer {bearer}\r\n"));
        }
        if let Some(body) = body {
            request.push_str("Content-Type: application/json\r\n");
            request.push_str(&format!("Content-Length: {}\r\n", body.len()));
        }
        request.push_str("\r\n");
        stream
            .write_all(request.as_bytes())
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        if let Some(body) = body {
            stream
                .write_all(body)
                .map_err(|_| UpstreamFailure::ConnectionError)?;
        }
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

    /// The readiness/auth probe budget: Python `AUTH_WATCH_PROBE_TIMEOUT_S`.
    ///
    /// A probe is a single `/api/models` read; the 15 s request default is too
    /// tight for a slow UM catalog and turned a transient timeout into a 503
    /// readiness flip even when the credential was still valid.
    pub fn get_json_probe(
        &self,
        path: &str,
        bearer: Option<&str>,
    ) -> Result<(u16, Option<Value>), UpstreamFailure> {
        let response = self.request("GET", path, bearer, None, PROBE_TIMEOUT)?;
        let payload = serde_json::from_slice(&response.body).ok();
        Ok((response.status, payload))
    }
}

fn read_upstream_response(stream: &mut dyn Read) -> Result<UpstreamResponse, UpstreamFailure> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let mut header_end = None;
    let mut content_length: Option<usize> = None;
    let mut chunked = false;
    loop {
        if let Some(end) = header_end {
            let body = &raw[end..];
            let complete = if chunked {
                body.ends_with(b"0\r\n\r\n")
            } else if let Some(length) = content_length {
                body.len() >= length
            } else {
                false
            };
            if complete {
                break;
            }
        }
        let read = match stream.read(&mut buffer) {
            Ok(read) => read,
            Err(error)
                if error.kind() == std::io::ErrorKind::TimedOut
                    || error.kind() == std::io::ErrorKind::WouldBlock =>
            {
                return Err(UpstreamFailure::Timeout)
            }
            // The upstream may close without a TLS close_notify after a complete
            // response; that is a clean EOF, like Python's requests.
            Err(error)
                if error.kind() == std::io::ErrorKind::UnexpectedEof
                    || error.kind() == std::io::ErrorKind::ConnectionReset
                    || error.kind() == std::io::ErrorKind::ConnectionAborted =>
            {
                break
            }
            Err(_) => return Err(UpstreamFailure::ConnectionError),
        };
        if read == 0 {
            if header_end.is_none() {
                return Err(UpstreamFailure::ConnectionError);
            }
            break;
        }
        raw.extend_from_slice(&buffer[..read]);
        if raw.len() > 64 * 1024 * 1024 {
            return Err(UpstreamFailure::ConnectionError);
        }
        if header_end.is_none() {
            if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
                header_end = Some(position + 4);
                let head = String::from_utf8_lossy(&raw[..position]);
                for line in head.split("\r\n").skip(1) {
                    if let Some((name, value)) = line.split_once(':') {
                        let name = name.trim().to_ascii_lowercase();
                        let value = value.trim();
                        if name == "content-length" {
                            content_length = value.parse::<usize>().ok();
                        }
                        if name == "transfer-encoding"
                            && value.to_ascii_lowercase().contains("chunked")
                        {
                            chunked = true;
                        }
                    }
                }
            }
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

/// The credential used for one upstream request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpstreamCredential {
    pub value: String,
    /// True when the value is the operator-saved Open WebUI API key.
    pub api_key: bool,
}

/// The last completed credential probe, kept for the durable auth state file.
#[derive(Debug, Clone)]
struct StoredCredentialObservation {
    checked_epoch: f64,
    verdict: &'static str,
    reason: Option<&'static str>,
    upstream_status: Option<u16>,
    summary: Option<String>,
}

/// The read-only proxy state shared by every request.
pub struct ProxyState {
    pub base_url: String,
    bearer: Arc<Mutex<String>>,
    api_key: Mutex<Option<String>>,
    api_key_rejected: AtomicBool,
    credential_observation: Mutex<Option<StoredCredentialObservation>>,
    last_valid_epoch: Mutex<Option<f64>>,
    last_auth_rejection_summary: Mutex<Option<String>>,
    pub build_id: String,
    upstream_config: Mutex<Option<Value>>,
    config_freshness: Mutex<ConfigFreshness>,
    upstream_state: Mutex<UpstreamState>,
    models_cache: Mutex<Option<(Instant, Value)>>,
    effort_models: Vec<String>,
    upstream: UpstreamClient,
    faults: UpstreamFaultLog,
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
            api_key: Mutex::new(None),
            api_key_rejected: AtomicBool::new(false),
            credential_observation: Mutex::new(None),
            last_valid_epoch: Mutex::new(None),
            last_auth_rejection_summary: Mutex::new(None),
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
            effort_models: config.effort_models.clone(),
            upstream: UpstreamClient::from_config(config)?,
            faults: UpstreamFaultLog::open(config.effective_upstream_fault_log()),
        })
    }

    /// One public `/api/config` observation, without the user's bearer.
    pub fn probe_config(&self) {
        let observed = self.upstream.get_json("/api/config", None);
        let (config, result, status, summary) = match observed {
            Ok((status, payload)) => {
                if status == 200 {
                    match payload {
                        Some(payload) if payload.is_object() => {
                            (Some(payload), "valid_config", Some(status), None)
                        }
                        _ => (None, "invalid_config_shape", Some(status), None),
                    }
                } else if status == 401 || status == 403 {
                    (None, "upstream_rejected", Some(status), None)
                } else if (500..=599).contains(&status) {
                    (
                        None,
                        "upstream_5xx",
                        Some(status),
                        summarize_payload(&payload),
                    )
                } else {
                    (
                        None,
                        "unexpected_status",
                        Some(status),
                        summarize_payload(&payload),
                    )
                }
            }
            Err(UpstreamFailure::Timeout) => (None, "timeout", None, None),
            Err(UpstreamFailure::ConnectionError) => (None, "network_error", None, None),
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
                "reason": result,
                "at_epoch": epoch_now(),
                "upstream_status": status,
            }));
            self.record_reachability("unknown", Some("config_probe_failed"), status);
        }
        drop(freshness);
        self.faults.record_probe(
            FaultSource::BackgroundProbe,
            result,
            status,
            summary,
            None,
        );
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
        if self.upstream_credential().value.is_empty() {
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
        self.readiness_snapshot_from(FaultSource::ActiveProbe)
    }

    /// One readiness observation attributed to its real source.
    ///
    /// The periodic auth watch uses [`FaultSource::BackgroundProbe`]; `/ready`
    /// and the explicit verify entry use [`FaultSource::ActiveProbe`]. The
    /// upstream fault record is updated here, and the response shape stays the
    /// frozen readiness contract.
    pub fn readiness_snapshot_from(&self, source: FaultSource) -> (bool, Value) {
        let observation = self.classify_credential();
        let (verdict, reason, upstream_status) = (
            observation.verdict,
            observation.reason,
            observation.upstream_status,
        );
        let reachability = match reason {
            Some("timeout") | Some("network_error") => "unreachable",
            _ if verdict == "needs_login" => "unknown",
            _ if upstream_status.is_some() => "ok",
            _ => "unknown",
        };
        self.record_reachability(reachability, reason, upstream_status);
        self.faults.record_probe(
            source,
            observation.result_label(),
            upstream_status,
            observation.summary,
            None,
        );
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

    fn classify_credential(&self) -> CredentialObservation {
        let credential = self.upstream_credential();
        if credential.value.is_empty() {
            return self.record_credential_observation(CredentialObservation {
                verdict: "needs_login",
                reason: Some("no_local_credential"),
                upstream_status: None,
                summary: None,
            });
        }
        let mut observation = self.probe_credential(&credential.value);
        if credential.api_key && observation.verdict == "rejected" {
            // An operator-saved API key may be stale. It must never wedge the
            // gateway: mark it rejected for evidence, then let the JWT bearer
            // try the same probe once.
            self.mark_api_key_rejected();
            if let Some(summary) = observation.summary.clone() {
                self.record_auth_rejection(observation.upstream_status.unwrap_or(0), Some(summary));
            }
            let bearer = self.bearer();
            if !bearer.is_empty() {
                observation = self.probe_credential(&bearer);
            }
        }
        self.record_credential_observation(observation)
    }

    fn probe_credential(&self, credential: &str) -> CredentialObservation {
        match self
            .upstream
            .get_json_probe("/api/models", Some(credential))
        {
            Err(UpstreamFailure::Timeout) => CredentialObservation {
                verdict: "unknown",
                reason: Some("timeout"),
                upstream_status: None,
                summary: None,
            },
            Err(UpstreamFailure::ConnectionError) => CredentialObservation {
                verdict: "unknown",
                reason: Some("network_error"),
                upstream_status: None,
                summary: None,
            },
            Ok((status, payload)) => {
                if status == 401 || status == 403 {
                    CredentialObservation {
                        verdict: "rejected",
                        reason: Some("upstream_rejected"),
                        upstream_status: Some(status),
                        summary: summarize_payload(&payload),
                    }
                } else if (500..=599).contains(&status) {
                    CredentialObservation {
                        verdict: "unknown",
                        reason: Some("upstream_5xx"),
                        upstream_status: Some(status),
                        summary: summarize_payload(&payload),
                    }
                } else if status != 200 {
                    CredentialObservation {
                        verdict: "unknown",
                        reason: Some("unexpected_status"),
                        upstream_status: Some(status),
                        summary: summarize_payload(&payload),
                    }
                } else if payload
                    .as_ref()
                    .and_then(Value::as_object)
                    .and_then(|payload| payload.get("data"))
                    .and_then(Value::as_array)
                    .is_none()
                {
                    CredentialObservation {
                        verdict: "unknown",
                        reason: Some("invalid_catalog_shape"),
                        upstream_status: Some(status),
                        summary: None,
                    }
                } else {
                    CredentialObservation {
                        verdict: "valid",
                        reason: None,
                        upstream_status: Some(status),
                        summary: None,
                    }
                }
            }
        }
    }

    fn record_credential_observation(
        &self,
        observation: CredentialObservation,
    ) -> CredentialObservation {
        let now = epoch_now();
        if observation.verdict == "valid" {
            *self.last_valid_epoch.lock().expect("last valid poisoned") = Some(now);
        }
        *self
            .credential_observation
            .lock()
            .expect("credential observation poisoned") = Some(StoredCredentialObservation {
            checked_epoch: now,
            verdict: observation.verdict,
            reason: observation.reason,
            upstream_status: observation.upstream_status,
            summary: observation.summary.clone(),
        });
        observation
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
        let credential = self.upstream_credential();
        let observed = self
            .upstream
            .get_json("/api/models", Some(&credential.value));
        let observed = match observed {
            Ok((status, payload)) if credential.api_key && matches!(status, 401 | 403) => {
                self.mark_api_key_rejected();
                self.record_auth_rejection(status, summarize_payload(&payload));
                let bearer = self.bearer();
                if bearer.is_empty() {
                    Ok((status, payload))
                } else {
                    self.upstream.get_json("/api/models", Some(&bearer))
                }
            }
            other => other,
        };
        let fetched = match observed {
            Ok((200, payload)) => {
                let valid = payload.as_ref().is_some_and(|payload| {
                    payload.is_object() && payload.get("data").and_then(Value::as_array).is_some()
                });
                if valid {
                    self.faults.record_recovery(
                        FaultSource::BusinessRequest,
                        Some(200),
                        "model_catalog",
                    );
                }
                payload.filter(|payload| {
                    payload.is_object() && payload.get("data").and_then(Value::as_array).is_some()
                })
            }
            Ok((status, payload)) if matches!(status, 401 | 403) => {
                self.record_auth_rejection(status, summarize_payload(&payload));
                return Err(GatewayError::reauthentication_required(Some(
                    "upstream_rejected",
                )));
            }
            Ok((status, payload)) => {
                if let Some(class) = fault_class_of_status(status) {
                    self.faults.record_failure(FaultObservation {
                        class,
                        upstream_status: Some(status),
                        summary: summarize_payload(&payload),
                        source: FaultSource::BusinessRequest,
                    });
                }
                None
            }
            Err(failure) => {
                let class = match failure {
                    UpstreamFailure::Timeout => FaultClass::Timeout,
                    UpstreamFailure::ConnectionError => FaultClass::NetworkError,
                };
                self.faults.record_failure(FaultObservation {
                    class,
                    upstream_status: None,
                    summary: None,
                    source: FaultSource::BusinessRequest,
                });
                None
            }
        };
        if let Some(payload) = fetched {
            *self.models_cache.lock().expect("models cache poisoned") =
                Some((Instant::now(), payload.clone()));
            if raw_fresh {
                return Ok((payload, true));
            }
            return Ok((append_effort_models(&payload, &self.effort_models), false));
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
            Some((_, payload)) => Ok((append_effort_models(&payload, &self.effort_models), false)),
            None => Err(GatewayError::models_unavailable()),
        }
    }

    /// One general `/api/*` passthrough with the stored upstream bearer.
    ///
    /// Python `Handler._passthrough` keeps the upstream status, copies only
    /// `content-type`/`cache-control` on success, and rebuilds an upstream
    /// 4xx/5xx in the unified error envelope with a bounded message.
    pub fn passthrough(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
    ) -> Result<Response, GatewayError> {
        let credential = self.upstream_credential();
        let mut response = self
            .upstream
            .request(
                method,
                path,
                Some(&credential.value),
                body,
                PASSTHROUGH_TIMEOUT,
            )
            .map_err(passthrough_failure)?;
        if credential.api_key && matches!(response.status, 401 | 403) {
            self.mark_api_key_rejected();
            self.record_auth_rejection(
                response.status,
                summarize_error_text(&String::from_utf8_lossy(&response.body)),
            );
            let bearer = self.bearer();
            if !bearer.is_empty() {
                response = self
                    .upstream
                    .request(method, path, Some(&bearer), body, PASSTHROUGH_TIMEOUT)
                    .map_err(passthrough_failure)?;
            }
        }
        if response.status >= 400 {
            let text = String::from_utf8_lossy(&response.body).to_string();
            return Err(GatewayError::upstream_status_error(
                response.status,
                Some(&text),
                response.headers.get("retry-after").map(String::as_str),
            ));
        }
        let mut result = Response::new(response.status, response.body);
        for name in ["content-type", "cache-control"] {
            if let Some(value) = response.headers.get(name) {
                result = result.with_header(name, value);
            }
        }
        Ok(result)
    }

    /// Whether the current bearer is accepted right now.
    pub fn credential_valid(&self) -> bool {
        self.classify_credential().verdict == "valid"
    }

    /// Publish the next periodic check as an interval from now.
    pub fn record_next_check_after(&self, interval: Duration) {
        self.faults
            .record_next_check(epoch_now() + interval.as_secs_f64());
    }

    /// Record contract-valid business success as recovery evidence.
    pub fn record_business_success(&self, detail: &str, upstream_status: Option<u16>) {
        self.faults.record_recovery(
            FaultSource::BusinessRequest,
            upstream_status,
            detail,
        );
    }

    /// Record one final business failure exactly once.
    pub fn record_business_failure(
        &self,
        class: crate::upstream_fault::FaultClass,
        upstream_status: Option<u16>,
        summary: Option<String>,
    ) {
        self.faults.record_failure(FaultObservation {
            class,
            upstream_status,
            summary,
            source: FaultSource::BusinessRequest,
        });
    }

    /// Record one final transport failure of a business request.
    pub fn record_business_transport_failure(
        &self,
        failure: crate::upstream::UpstreamFailure,
    ) {
        let class = match failure {
            crate::upstream::UpstreamFailure::Timeout => FaultClass::Timeout,
            crate::upstream::UpstreamFailure::ConnectionError
            | crate::upstream::UpstreamFailure::Protocol => FaultClass::NetworkError,
        };
        self.record_business_failure(class, None, None);
    }

    /// The current in-memory bearer.
    pub fn bearer(&self) -> String {
        self.bearer.lock().expect("bearer poisoned").clone()
    }

    /// The preferred upstream credential: the saved API key first, the JWT bearer
    /// second. A rejected API key is skipped until a new one is published or the
    /// process restarts.
    pub fn upstream_credential(&self) -> UpstreamCredential {
        let api_key = self
            .api_key
            .lock()
            .expect("api key poisoned")
            .clone()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        if let Some(api_key) = api_key {
            if !self.api_key_rejected.load(Ordering::SeqCst) {
                return UpstreamCredential {
                    value: api_key,
                    api_key: true,
                };
            }
        }
        UpstreamCredential {
            value: self.bearer(),
            api_key: false,
        }
    }

    /// Store the operator-saved API key loaded from `token.json`.
    pub fn set_api_key(&self, api_key: Option<&str>) {
        let normalized = api_key
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        *self.api_key.lock().expect("api key poisoned") = normalized;
        self.api_key_rejected.store(false, Ordering::SeqCst);
    }

    /// Mark the operator-saved API key as rejected by the upstream.
    pub fn mark_api_key_rejected(&self) {
        self.api_key_rejected.store(true, Ordering::SeqCst);
    }

    /// Whether an operator-saved API key exists (never its value).
    pub fn api_key_present(&self) -> bool {
        self.api_key
            .lock()
            .expect("api key poisoned")
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
    }

    /// Whether the operator-saved API key was rejected by the upstream.
    pub fn api_key_rejected(&self) -> bool {
        self.api_key_rejected.load(Ordering::SeqCst)
    }

    /// Record one 401/403 observation without storing the credential.
    pub fn record_auth_rejection(&self, _status: u16, summary: Option<String>) {
        if let Some(summary) = summary {
            *self
                .last_auth_rejection_summary
                .lock()
                .expect("auth rejection summary poisoned") = Some(summary);
        }
    }

    /// The latest credential evidence, without any credential value.
    pub fn credential_evidence(&self) -> CredentialEvidence {
        let observation = self
            .credential_observation
            .lock()
            .expect("credential observation poisoned")
            .clone();
        CredentialEvidence {
            checked_epoch: observation
                .as_ref()
                .map(|observation| observation.checked_epoch),
            verdict: observation
                .as_ref()
                .map(|observation| observation.verdict.to_string()),
            reason: observation
                .as_ref()
                .and_then(|observation| observation.reason.map(str::to_string)),
            upstream_status: observation
                .as_ref()
                .and_then(|observation| observation.upstream_status),
            last_valid_epoch: *self.last_valid_epoch.lock().expect("last valid poisoned"),
            upstream_401_summary: self
                .last_auth_rejection_summary
                .lock()
                .expect("auth rejection summary poisoned")
                .clone()
                .or_else(|| {
                    observation
                        .as_ref()
                        .and_then(|observation| observation.summary.clone())
                }),
            api_key_present: self.api_key_present(),
            api_key_rejected: self.api_key_rejected(),
        }
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
        let observation = self.classify_credential();
        self.faults.record_probe(
            FaultSource::ActiveProbe,
            observation.result_label(),
            observation.upstream_status,
            observation.summary,
            None,
        );
        json!({
            "verify": observation.verdict,
            "reason": observation.reason,
            "upstream_status": observation.upstream_status,
            "checked_epoch": epoch_now() as u64,
            "note": "single-shot upstream /api/models probe; auth state and model cache untouched",
        })
    }
}

struct CredentialObservation {
    verdict: &'static str,
    reason: Option<&'static str>,
    upstream_status: Option<u16>,
    summary: Option<String>,
}

impl CredentialObservation {
    fn result_label(&self) -> &'static str {
        if self.verdict == "valid" {
            "valid"
        } else {
            self.reason.unwrap_or("unknown")
        }
    }
}

fn summarize_payload(payload: &Option<Value>) -> Option<String> {
    payload
        .as_ref()
        .and_then(|payload| summarize_error_text(&payload.to_string()))
}

impl CredentialSink for ProxyState {
    fn publish_credential(&self, token: &str, api_key: Option<&str>) {
        self.set_bearer(token);
        if let Some(api_key) = api_key {
            let api_key = api_key.trim();
            let mut stored = self.api_key.lock().expect("api key poisoned");
            let changed = stored.as_deref() != (!api_key.is_empty()).then_some(api_key);
            *stored = (!api_key.is_empty()).then(|| api_key.to_string());
            drop(stored);
            if changed {
                self.api_key_rejected.store(false, Ordering::SeqCst);
            }
        }
        self.invalidate_models();
    }

    fn credential_evidence(&self) -> CredentialEvidence {
        self.credential_evidence()
    }
}

/// Python `Proxy.list_models` default catalog: append one virtual variant per
/// configured base model that is actually present in the upstream snapshot. A base
/// that disappeared never exposes selectable variants, and an existing variant id
/// is never duplicated.
fn append_effort_models(payload: &Value, bases: &[String]) -> Value {
    let mut payload = payload.clone();
    let Some(items) = payload
        .as_object_mut()
        .and_then(|object| object.get_mut("data"))
        .and_then(Value::as_array_mut)
    else {
        return payload;
    };
    let sources = items.clone();
    for base in bases {
        let Some(source) = sources
            .iter()
            .find(|model| model.get("id").and_then(Value::as_str) == Some(base.as_str()))
        else {
            continue;
        };
        for (suffix, _effort) in EFFORT_LEVELS {
            let variant_id = format!("{base}-{suffix}");
            if items
                .iter()
                .any(|model| model.get("id").and_then(Value::as_str) == Some(variant_id.as_str()))
            {
                continue;
            }
            let mut variant = source.clone();
            if let Some(object) = variant.as_object_mut() {
                object.insert("id".to_string(), Value::String(variant_id.clone()));
                object.insert("name".to_string(), Value::String(variant_id));
            }
            items.push(variant);
        }
    }
    payload
}

fn passthrough_failure(failure: UpstreamFailure) -> GatewayError {
    match failure {
        UpstreamFailure::Timeout => GatewayError::upstream_timeout_with_reason(failure.name()),
        UpstreamFailure::ConnectionError => {
            GatewayError::upstream_transport_failure(failure.name())
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_chunked_response_stops_at_the_terminator() {
        let server = um_test_harness::TestServer::start(|request| {
            assert_eq!(request.path, "/v1/models");
            um_test_harness::HttpResponse::new(200, b"5\r\nhello\r\n0\r\n\r\n".to_vec())
                .with_header("transfer-encoding", "chunked")
        })
        .expect("upstream server");
        let config = GatewayConfig {
            upstream_base_url: format!("http://127.0.0.1:{}", server.port()),
            ..GatewayConfig::default()
        };
        let client = UpstreamClient::from_config(&config).expect("upstream client");
        let (status, payload) = client.get_json_probe("/v1/models", None).expect("probe");
        assert_eq!(status, 200);
        assert!(payload.is_none(), "chunked hello is not a JSON object");
    }

    #[test]
    fn a_content_length_response_is_read_without_waiting_for_close() {
        let server = um_test_harness::TestServer::start(|request| {
            assert_eq!(request.path, "/v1/models");
            um_test_harness::HttpResponse::json(200, r#"{"data":[{"id":"fixture-model"}]}"#)
        })
        .expect("upstream server");
        let config = GatewayConfig {
            upstream_base_url: format!("http://127.0.0.1:{}", server.port()),
            ..GatewayConfig::default()
        };
        let client = UpstreamClient::from_config(&config).expect("upstream client");
        let (status, payload) = client.get_json_probe("/v1/models", None).expect("probe");
        assert_eq!(status, 200);
        assert_eq!(payload.expect("payload")["data"][0]["id"], json!("fixture-model"));
    }

    use super::*;

    fn proxy() -> ProxyState {
        let config = GatewayConfig {
            upstream_base_url: "http://127.0.0.1:9".to_string(),
            ..GatewayConfig::default()
        };
        ProxyState::new(&config).expect("proxy state")
    }

    #[test]
    fn publish_credential_writes_the_api_key_and_keeps_it_without_a_new_one() {
        let proxy = proxy();
        assert_eq!(
            proxy.upstream_credential(),
            UpstreamCredential {
                value: String::new(),
                api_key: false,
            }
        );
        proxy.publish_credential("jwt-token", Some("api-key"));
        assert_eq!(
            proxy.upstream_credential(),
            UpstreamCredential {
                value: "api-key".to_string(),
                api_key: true,
            }
        );
        proxy.publish_credential("jwt-token-2", None);
        assert_eq!(
            proxy.upstream_credential(),
            UpstreamCredential {
                value: "api-key".to_string(),
                api_key: true,
            },
            "登录提交新 token 时不得清掉已保存的 API key"
        );
    }

    #[test]
    fn a_rejected_api_key_is_skipped_until_a_new_value_is_published() {
        let proxy = proxy();
        proxy.publish_credential("jwt-token", Some("api-key"));
        proxy.mark_api_key_rejected();
        assert_eq!(
            proxy.upstream_credential(),
            UpstreamCredential {
                value: "jwt-token".to_string(),
                api_key: false,
            }
        );
        proxy.publish_credential("jwt-token-2", Some("api-key"));
        assert!(!proxy.upstream_credential().api_key);
        proxy.publish_credential("jwt-token-3", Some("new-key"));
        assert_eq!(
            proxy.upstream_credential(),
            UpstreamCredential {
                value: "new-key".to_string(),
                api_key: true,
            }
        );
    }
}
