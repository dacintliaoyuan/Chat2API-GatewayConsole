//! #111: the production `ShareWriteSeams` over the broker management channel.
//!
//! The console publishes the rendered projection through the REAL management
//! wire contract frozen in `docs/rust-contract.md` §7: one signed schema-v2
//! request per exchange, a one-shot `request_id`, HMAC-SHA256 over the fixed
//! signed keys, a bounded AF_UNIX exchange and an explicit ACK check. Every
//! transport, parse or verification failure is `ShareWriteError::broker` (503);
//! a fabricated success is never returned.
//!
//! The endpoint is POSIX-only. On Windows the seam keeps the honest unwired
//! refusal, and `ConsoleServer::start` only replaces the unwired default when
//! both the socket and the control key are present and readable.

#![cfg(unix)]

use std::net::ToSocketAddrs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::share_write::{ShareWriteError, ShareWriteSeams, UnwiredWriteSeams};

/// The wire default from `docs/rust-contract.md` §7.3 (10 s).
pub const PUBLISH_DEADLINE: Duration = Duration::from_secs(10);
/// The broker's emergency stop budget plus the reply margin; the seam never
/// exceeds the frozen 20 s upper bound.
pub const EMERGENCY_STOP_DEADLINE: Duration = Duration::from_secs(20);
/// `share/broker_server.py::read_control_key` refuses a key larger than this.
pub const MAX_CONTROL_KEY_BYTES: u64 = 128;
pub const MAX_TERMINATE_KEYS: usize = 16;

/// The production seam: signed management requests over AF_UNIX.
///
/// The constructor receives the already-read control secret so `publish` and
/// `emergency_stop` never touch the key file again. `from_paths` is the
/// fail-closed assembly check used by `ConsoleServer::start`.
pub struct BrokerManagementSeams {
    socket_path: PathBuf,
    secret: String,
    /// The gateway loopback port the private Key probe addresses.
    gateway_port: u16,
}

impl BrokerManagementSeams {
    pub fn new(socket_path: PathBuf, secret: String, gateway_port: u16) -> Self {
        Self {
            socket_path,
            secret,
            gateway_port,
        }
    }

    /// Build the seam only when the socket endpoint exists and the control key
    /// is readable by the current user. Any missing, foreign or wider key file
    /// keeps the unwired default instead of degrading the key gate.
    pub fn from_paths(
        socket_path: Option<&Path>,
        control_key_file: Option<&Path>,
        gateway_port: u16,
    ) -> Option<Self> {
        use std::os::unix::fs::FileTypeExt;

        let socket_path = socket_path?;
        let control_key_file = control_key_file?;
        let metadata = std::fs::symlink_metadata(socket_path).ok()?;
        if !metadata.file_type().is_socket() {
            return None;
        }
        let secret = read_control_key(control_key_file).ok()?;
        Some(Self::new(socket_path.to_path_buf(), secret, gateway_port))
    }

    fn exchange(
        &self,
        action: &str,
        fields: Value,
        deadline: Duration,
    ) -> Result<um_broker::error::BrokerResult, ShareWriteError> {
        self.exchange_unix(action, fields, deadline)
    }

    fn exchange_unix(
        &self,
        action: &str,
        fields: Value,
        deadline: Duration,
    ) -> Result<um_broker::error::BrokerResult, ShareWriteError> {
        use um_broker::error::{BrokerResult, ResultCode};
        use um_broker::server::FrameIo;

        let deadline = Instant::now() + deadline;
        let (request_id, frame) = management_request(&self.secret, action, fields)?;
        let mut connection = connect_bounded(&self.socket_path, deadline)?;
        connection.write_frame(&frame, deadline).map_err(|error| {
            ShareWriteError::broker(format!("broker {action} request failed: {error}"))
        })?;
        let line = connection.read_frame(deadline).map_err(|error| {
            ShareWriteError::broker(format!("broker {action} reply failed: {error}"))
        })?;
        let reply = um_broker::codec::decode_frame(&line).map_err(|error| {
            ShareWriteError::broker(format!(
                "broker {action} reply is not a valid frame: {}",
                error.message()
            ))
        })?;
        if reply.get("request_id").and_then(Value::as_str) != Some(request_id.as_str()) {
            return Err(ShareWriteError::broker(format!(
                "broker {action} reply request_id does not match the request"
            )));
        }
        let result = BrokerResult::from_value(&reply);
        if !result.ok_flag() || result.error_code() != ResultCode::Ok {
            let detail = result
                .result()
                .get("detail")
                .and_then(Value::as_str)
                .unwrap_or_default();
            return Err(ShareWriteError::broker(format!(
                "broker {action} failed ({}): {detail}",
                result.error_code()
            )));
        }
        Ok(result)
    }
}

impl ShareWriteSeams for BrokerManagementSeams {
    /// `BrokerClientAdapter.host_verify("running")`: three-state evidence.
    /// The broker's own `verify_running` uses the 10 s wire deadline; the client
    /// adds connection and reply margin so a slow broker is not reported as an
    /// unconfirmed stop.
    fn gateway_running(&self) -> Option<bool> {
        let mut fields = Map::new();
        fields.insert("check".to_string(), json!("running"));
        let result = self
            .exchange("host-verify", Value::Object(fields), EMERGENCY_STOP_DEADLINE)
            .ok()?;
        result.result().get("host_running").and_then(Value::as_bool)
    }

    /// The explicit version-bound public probe: resolve the public origin, read
    /// `/v1/models`, then run one minimal non-stream inference. The Key
    /// plaintext stays in this process; only the bounded outcome is returned.
    fn probe_models(&self, secret: &str, public_url: &str) -> Option<Value> {
        let started = std::time::Instant::now();
        let public_ip = match resolve_public_ip(public_url) {
            Ok(public_ip) => public_ip,
            Err(reason) => return Some(probe_failure("models", reason, None)),
        };
        let config = um_gateway::config::GatewayConfig {
            upstream_base_url: public_url.to_string(),
            upstream_timeout: Duration::from_secs(20),
            ..um_gateway::config::GatewayConfig::default()
        };
        let Ok(client) = um_gateway::readonly::UpstreamClient::from_config(&config) else {
            return Some(probe_failure("models", "public_url_invalid", None));
        };
        let (status, payload) = match client.get_json_probe("/v1/models", Some(secret)) {
            Ok(observed) => observed,
            Err(um_gateway::readonly::UpstreamFailure::Timeout) => {
                return Some(probe_failure("models", "timeout", None))
            }
            Err(_) => return Some(probe_failure("models", "public_url_unreachable", None)),
        };
        if status != 200 {
            return Some(probe_failure("models", "http_error", Some(status)));
        }
        let model = match pick_model(&payload, secret) {
            Ok(model) => model,
            Err(reason) => return Some(probe_failure("models", reason, Some(status))),
        };
        let body = json!({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": false,
        });
        let Ok(body) = serde_json::to_vec(&body) else {
            return Some(probe_failure("inference", "invalid_request", None));
        };
        let response = match client.request(
            "POST",
            "/v1/chat/completions",
            Some(secret),
            Some(&body),
            Duration::from_secs(60),
        ) {
            Ok(response) => response,
            Err(um_gateway::readonly::UpstreamFailure::Timeout) => {
                return Some(probe_failure("inference", "timeout", None))
            }
            Err(_) => return Some(probe_failure("inference", "public_url_unreachable", None)),
        };
        if response.status != 200 {
            return Some(probe_failure("inference", "http_error", Some(response.status)));
        }
        let payload = serde_json::from_slice(&response.body).ok();
        if !valid_inference(&payload) {
            return Some(probe_failure(
                "inference",
                "invalid_response",
                Some(response.status),
            ));
        }
        Some(json!({
            "connected": true,
            "reason": "ok",
            "stage": "inference",
            "http_status": 200,
            "model": model,
            "public_ip": public_ip,
            "dns": {"address": public_ip, "source": "resolved"},
            "elapsed_ms": started.elapsed().as_millis() as u64,
        }))
    }

    /// The private loopback probe: one Key-authenticated `/v1/models` read.
    fn probe_models_private(&self, secret: &str) -> Option<Value> {
        let started = std::time::Instant::now();
        let config = um_gateway::config::GatewayConfig {
            upstream_base_url: format!("http://127.0.0.1:{}", self.gateway_port),
            upstream_timeout: Duration::from_secs(20),
            ..um_gateway::config::GatewayConfig::default()
        };
        let Ok(client) = um_gateway::readonly::UpstreamClient::from_config(&config) else {
            return Some(probe_failure("models", "gateway_unreachable", None));
        };
        let (status, payload) = match client.get_json_probe("/v1/models", Some(secret)) {
            Ok(observed) => observed,
            Err(um_gateway::readonly::UpstreamFailure::Timeout) => {
                return Some(probe_failure("models", "timeout", None))
            }
            Err(_) => {
                return Some(probe_failure("models", "gateway_unreachable", None));
            }
        };
        if status != 200 {
            return Some(probe_failure("models", "http_error", Some(status)));
        }
        let model = match pick_model(&payload, secret) {
            Ok(model) => model,
            Err(reason) => return Some(probe_failure("models", reason, Some(status))),
        };
        Some(json!({
            "connected": true,
            "reason": "ok",
            "stage": "models",
            "http_status": 200,
            "model": model,
            "elapsed_ms": started.elapsed().as_millis() as u64,
        }))
    }

    fn publish(
        &self,
        projection: &Value,
        terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError> {
        let generation = projection
            .get("config_generation")
            .and_then(Value::as_i64)
            .filter(|value| *value >= 1)
            .ok_or_else(|| {
                ShareWriteError::broker("projection config_generation must be a positive integer")
            })?;
        let fingerprint = um_store::fingerprint(projection);
        let mut fields = Map::new();
        fields.insert("generation".to_string(), json!(generation));
        fields.insert(
            "fingerprint".to_string(),
            Value::String(fingerprint.clone()),
        );
        fields.insert("projection".to_string(), projection.clone());
        if let Some(keys) = terminate_keys.filter(|keys| !keys.is_empty()) {
            let keys: Vec<Value> = keys
                .iter()
                .take(MAX_TERMINATE_KEYS)
                .map(|key| Value::String(key.clone()))
                .collect();
            fields.insert("terminate_keys".to_string(), Value::Array(keys));
        }
        let result = self.exchange("publish", Value::Object(fields), PUBLISH_DEADLINE)?;
        let ack = result.result();
        let ack_generation = ack.get("generation").and_then(Value::as_i64);
        if ack_generation != Some(generation) {
            return Err(ShareWriteError::broker(format!(
                "broker ack generation {ack_generation:?} does not match target {generation}"
            )));
        }
        if ack.get("fingerprint").and_then(Value::as_str) != Some(fingerprint.as_str()) {
            return Err(ShareWriteError::broker(
                "broker ack fingerprint does not match the publication",
            ));
        }
        let Some(instance_id) = ack
            .get("instance_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        else {
            return Err(ShareWriteError::broker(
                "broker ack does not identify the acking instance",
            ));
        };
        let mut output = Map::new();
        output.insert("applied_generation".to_string(), json!(generation));
        output.insert("instance_id".to_string(), json!(instance_id));
        output.insert("fingerprint".to_string(), json!(fingerprint));
        for name in ["terminated", "released", "aborted", "upstream_closed"] {
            if let Some(value) = ack.get(name) {
                output.insert(name.to_string(), value.clone());
            }
        }
        if let Some(value) = ack.get("security_generation") {
            output.insert("security_generation".to_string(), value.clone());
        }
        Ok(Value::Object(output))
    }

    fn emergency_stop(&self) -> Result<Value, ShareWriteError> {
        let result = self.exchange(
            "emergency-stop",
            Value::Object(Map::new()),
            EMERGENCY_STOP_DEADLINE,
        )?;
        let payload = result.result();
        if payload.get("stopped").and_then(Value::as_bool).is_none() {
            return Err(ShareWriteError::broker(
                "broker emergency-stop result did not include a stop confirmation",
            ));
        }
        Ok(payload.clone())
    }
}

/// The production assembly for `ConsoleServer::start`: the broker seam only
/// replaces the unwired default when the deployment paths are readable.
pub fn production_write_seams(config: &ConsoleConfig) -> std::sync::Arc<dyn ShareWriteSeams> {
    if let Some(seams) = BrokerManagementSeams::from_paths(
        config.broker_socket_path.as_deref(),
        config.broker_control_key_file.as_deref(),
        config.gateway_port,
    ) {
        return std::sync::Arc::new(seams);
    }
    std::sync::Arc::new(UnwiredWriteSeams)
}

/// Known-good model ids preferred over the first catalog entry.
pub const PREFERRED_MODEL_IDS: &[&str] = &["Qwen3.5-397B-A17B", "DeepSeek-V4.1-Flash-1017"];

/// One structured probe failure; `None` is never flattened into a bare
/// connection error, so the UI can tell timeout, DNS, HTTP and shape failures
/// apart.
fn probe_failure(stage: &str, reason: &str, http_status: Option<u16>) -> Value {
    json!({
        "connected": false,
        "reason": reason,
        "stage": stage,
        "http_status": http_status,
    })
}

/// The first usable model id, preferring a known-good id. Empty, oversized,
/// control-character or Key-bearing ids are never returned.
fn pick_model(payload: &Option<Value>, secret: &str) -> Result<String, &'static str> {
    let models = payload
        .as_ref()
        .and_then(|payload| payload.get("data"))
        .and_then(Value::as_array)
        .ok_or("invalid_response")?;
    let usable = |model: &&Value| {
        model
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(|id| {
                !id.is_empty()
                    && id.chars().count() <= 200
                    && !id.chars().any(char::is_control)
                    && !secret.is_empty()
                    && !id.contains(secret)
            })
    };
    for preferred in PREFERRED_MODEL_IDS {
        if models
            .iter()
            .filter(usable)
            .any(|model| model.get("id").and_then(Value::as_str) == Some(preferred))
        {
            return Ok(preferred.to_string());
        }
    }
    models
        .iter()
        .filter(usable)
        .filter_map(|model| model.get("id").and_then(Value::as_str))
        .next()
        .map(str::to_string)
        .ok_or("no_models")
}

/// A successful inference carries an assistant message with non-empty content and
/// no `error` field.
fn valid_inference(payload: &Option<Value>) -> bool {
    let Some(choices) = payload
        .as_ref()
        .and_then(|payload| payload.get("choices"))
        .and_then(Value::as_array)
    else {
        return false;
    };
    let Some(first) = choices.first() else {
        return false;
    };
    if first.get("error").is_some() {
        return false;
    }
    let Some(message) = first.get("message") else {
        return false;
    };
    message.get("role").and_then(Value::as_str) == Some("assistant")
        && message
            .get("content")
            .and_then(Value::as_str)
            .is_some_and(|content| !content.trim().is_empty())
}

/// Resolve the public origin to a global IPv4; loopback, private, link-local and
/// unspecified addresses are never accepted as public evidence.
fn resolve_public_ip(public_url: &str) -> Result<String, &'static str> {
    let host = public_url
        .trim_start_matches("https://")
        .trim_start_matches("http://")
        .split('/')
        .next()
        .unwrap_or_default()
        .split(':')
        .next()
        .unwrap_or_default();
    if host.is_empty() {
        return Err("public_url_invalid");
    }
    let addresses = (host, 443)
        .to_socket_addrs()
        .map_err(|_| "public_dns_failed")?;
    for address in addresses {
        if let std::net::IpAddr::V4(ip) = address.ip() {
            if !ip.is_loopback() && !ip.is_private() && !ip.is_link_local() && !ip.is_unspecified()
            {
                return Ok(ip.to_string());
            }
        }
    }
    Err("public_dns_failed")
}

fn management_request(
    secret: &str,
    action: &str,
    fields: Value,
) -> Result<(String, Vec<u8>), ShareWriteError> {
    let request_id = one_shot_request_id()?;
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ShareWriteError::broker("broker request clock is before the epoch"))?
        .as_secs() as i64;
    let mut body = Map::new();
    body.insert("v".to_string(), Value::from(2));
    body.insert("action".to_string(), Value::String(action.to_string()));
    body.insert("request_id".to_string(), Value::String(request_id.clone()));
    body.insert("ts".to_string(), Value::from(ts));
    body.insert("fields".to_string(), fields);
    um_broker::hmac::sign_value(
        secret,
        &mut body,
        &um_broker::request::MANAGEMENT_SIGNED_KEYS,
    );
    let frame = um_broker::codec::encode_frame(&Value::Object(body)).map_err(|error| {
        ShareWriteError::broker(format!(
            "broker request encoding failed: {}",
            error.message()
        ))
    })?;
    Ok((request_id, frame))
}

fn one_shot_request_id() -> Result<String, ShareWriteError> {
    let mut bytes = [0u8; 16];
    getrandom::getrandom(&mut bytes)
        .map_err(|_| ShareWriteError::broker("broker request id generation failed"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn connect_bounded(
    path: &Path,
    deadline: Instant,
) -> Result<um_broker::transport::unix::UnixConnection, ShareWriteError> {
    let path = path.to_path_buf();
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        // The worker owns the connection until it is handed to the exchange;
        // a timed-out connect drops it here instead of leaking a usable socket.
        let _ = sender.send(um_broker::transport::unix::UnixConnection::connect(&path));
    });
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return Err(ShareWriteError::broker(
            "broker exchange deadline exceeded before connect",
        ));
    }
    match receiver.recv_timeout(remaining) {
        Ok(Ok(connection)) => Ok(connection),
        Ok(Err(error)) => Err(ShareWriteError::broker(format!(
            "broker connect failed: {error}"
        ))),
        Err(_) => Err(ShareWriteError::broker(
            "broker connect exceeded the exchange deadline",
        )),
    }
}

fn read_control_key(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "broker control key is not a regular file",
        ));
    }
    if metadata.mode() & 0o777 != 0o600 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "broker control key must have mode 0600",
        ));
    }
    // SAFETY: `geteuid` has no preconditions.
    if metadata.uid() != unsafe { libc::geteuid() } {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "broker control key is not owned by the current uid",
        ));
    }
    let mut raw = Vec::new();
    file.take(MAX_CONTROL_KEY_BYTES + 1).read_to_end(&mut raw)?;
    if raw.len() as u64 > MAX_CONTROL_KEY_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "broker control key is unexpectedly large",
        ));
    }
    let text = String::from_utf8(raw).map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "broker control key is not UTF-8",
        )
    })?;
    let secret = text.trim().to_string();
    if secret.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "broker control key is empty",
        ));
    }
    Ok(secret)
}
