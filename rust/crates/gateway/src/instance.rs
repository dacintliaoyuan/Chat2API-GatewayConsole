//! Broker instance channel (schema v2) for the production gateway.
//!
//! The broker's `publish`, `drain` and `emergency` verbs address this process
//! over an AF_UNIX socket (`<prefix><instance_id>`) and expect a signed ACK.
//! The share gate re-reads the projection file for every request, so `load` only
//! terminates the one-shot revoked keys and drain/resume/emergency flip the
//! admission state; the instance never re-reads or rewrites the projection.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::gate::{constant_time_eq, GateState, ShareGate};

/// Wire schema version shared with `share/broker_protocol.py`.
pub const SCHEMA_VERSION: i64 = 2;
/// Request timestamp window in seconds.
pub const TS_WINDOW_SECONDS: i64 = 300;
/// Bound of one request frame.
pub const MAX_MESSAGE: usize = 1024 * 1024;

const INSTANCE_ACTIONS: [&str; 5] = ["instance-status", "load", "drain", "resume", "emergency"];
const INSTANCE_SIGNED_KEYS: [&str; 6] =
    ["v", "action", "request_id", "ts", "instance_id", "fields"];

/// One instance-channel handler over the share gate.
pub struct InstanceHandler {
    gate: Arc<ShareGate>,
    instance_id: String,
    secret: String,
    replay: Mutex<HashMap<String, i64>>,
}

impl InstanceHandler {
    pub fn new(gate: Arc<ShareGate>, secret: String) -> Self {
        Self {
            gate,
            instance_id: random_instance_id(),
            secret,
            replay: Mutex::new(HashMap::new()),
        }
    }

    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    /// Validate and dispatch one decoded request frame.
    pub fn handle(&self, body: &Value, now: i64) -> Value {
        let request_id = body
            .get("request_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let refusal = |code: &str, detail: &str| {
            json!({
                "v": SCHEMA_VERSION,
                "request_id": request_id,
                "ok": false,
                "error_code": code,
                "result": {"detail": detail},
            })
        };
        let Some(object) = body.as_object() else {
            return refusal("bad_request", "request must be an object");
        };
        if object.get("v").and_then(Value::as_i64) != Some(SCHEMA_VERSION) {
            return refusal("bad_request", "unsupported protocol version");
        }
        let Some(action) = object.get("action").and_then(Value::as_str) else {
            return refusal("bad_request", "unknown action");
        };
        if !INSTANCE_ACTIONS.contains(&action) {
            return refusal("bad_request", "unknown action");
        }
        let Some(ts) = object.get("ts").and_then(Value::as_i64) else {
            return refusal("bad_request", "request must include integer ts");
        };
        if (ts - now).abs() > TS_WINDOW_SECONDS {
            return refusal("stale_ts", "request timestamp outside window");
        }
        let request_id = object
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty() && value.len() <= 128)
            .unwrap_or_default();
        if request_id.is_empty() {
            return refusal("bad_request", "invalid request_id");
        }
        let request_id = request_id.to_string();
        match object.get("instance_id").and_then(Value::as_str) {
            Some(instance_id) if instance_id == self.instance_id => {}
            _ => {
                return refusal("instance_mismatch", "request targets another instance");
            }
        }
        let expected = hmac_sha256(
            self.secret.as_bytes(),
            canonical(&Value::Object(signed_payload(object))).as_bytes(),
        );
        let supplied = object
            .get("mac")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !constant_time_eq(expected.as_bytes(), supplied.as_bytes()) {
            return refusal("forbidden", "forged or missing mac");
        }
        let Some(fields) = object.get("fields").and_then(Value::as_object) else {
            return refusal("bad_request", "fields must be an object");
        };
        {
            let mut replay = self.replay.lock().expect("instance replay poisoned");
            replay.retain(|_, seen| now - *seen <= TS_WINDOW_SECONDS);
            if replay.contains_key(&request_id) {
                return refusal("replay", "request id already consumed");
            }
            replay.insert(request_id.clone(), ts);
        }
        match self.dispatch(action, fields) {
            Ok(result) => json!({
                "v": SCHEMA_VERSION,
                "request_id": request_id,
                "ok": true,
                "error_code": "ok",
                "result": result,
            }),
            Err((code, detail)) => refusal(code, detail),
        }
    }

    fn dispatch(
        &self,
        action: &str,
        fields: &Map<String, Value>,
    ) -> Result<Value, (&'static str, &'static str)> {
        match action {
            "instance-status" => {
                if !fields.is_empty() {
                    return Err(("bad_request", "unknown field(s)"));
                }
                let snapshot = self.gate.snapshot();
                Ok(json!({
                    "instance_id": self.instance_id,
                    "state": snapshot["state"],
                    "active_count": active_count(&snapshot),
                    "last_seen_generation": snapshot["config_generation"],
                }))
            }
            "load" => {
                if fields.keys().any(|key| {
                    !matches!(
                        key.as_str(),
                        "generation" | "fingerprint" | "terminate_keys"
                    )
                }) {
                    return Err(("bad_request", "unknown field(s)"));
                }
                let generation = match fields.get("generation") {
                    Some(value) if value.as_i64().is_some_and(|value| value >= 1) => value.clone(),
                    _ => return Err(("bad_request", "generation must be a positive integer")),
                };
                let fingerprint = match fields.get("fingerprint").and_then(Value::as_str) {
                    Some(value) if !value.is_empty() && value.len() <= 128 => value.to_string(),
                    _ => return Err(("bad_request", "fingerprint must be a 1..128 char string")),
                };
                let mut released = 0;
                if let Some(keys) = fields.get("terminate_keys") {
                    let Some(keys) = keys.as_array() else {
                        return Err(("bad_request", "terminate_keys must be a list"));
                    };
                    if keys.is_empty() || keys.len() > 16 {
                        return Err(("bad_request", "terminate_keys must be a 1..16 item list"));
                    }
                    for key in keys {
                        let Some(key) = key
                            .as_str()
                            .filter(|key| !key.is_empty() && key.len() <= 64)
                        else {
                            return Err((
                                "bad_request",
                                "terminate key must be a 1..64 char string",
                            ));
                        };
                        released += self.gate.terminate_key(key);
                    }
                }
                let snapshot = self.gate.snapshot();
                Ok(json!({
                    "instance_id": self.instance_id,
                    "generation": generation,
                    "fingerprint": fingerprint,
                    "installed": true,
                    "state": snapshot["state"],
                    "share_max_concurrency": snapshot["share_max_concurrency"],
                    "security_generation": snapshot["config_generation"],
                    "released": released,
                }))
            }
            "drain" => {
                if !fields.is_empty() {
                    return Err(("bad_request", "unknown field(s)"));
                }
                self.gate.set_state(GateState::Draining);
                let snapshot = self.gate.snapshot();
                Ok(json!({
                    "instance_id": self.instance_id,
                    "state": "draining",
                    "active_count": active_count(&snapshot),
                }))
            }
            "resume" => {
                if !fields.is_empty() {
                    return Err(("bad_request", "unknown field(s)"));
                }
                self.gate.set_state(GateState::Running);
                let snapshot = self.gate.snapshot();
                Ok(json!({
                    "instance_id": self.instance_id,
                    "state": "running",
                    "active_count": active_count(&snapshot),
                }))
            }
            "emergency" => {
                if !fields.is_empty() {
                    return Err(("bad_request", "unknown field(s)"));
                }
                let snapshot = self.gate.snapshot();
                let cleared = active_count(&snapshot);
                if let Some(active) = snapshot["active"].as_object() {
                    for key_id in active.keys() {
                        self.gate.terminate_key(key_id);
                    }
                }
                self.gate.set_state(GateState::Stopped);
                Ok(json!({
                    "instance_id": self.instance_id,
                    "state": "stopped",
                    "cleared_in_flight": cleared,
                }))
            }
            _ => Err(("bad_request", "unknown instance action")),
        }
    }
}

fn active_count(snapshot: &Value) -> i64 {
    snapshot["active"]
        .as_object()
        .map(|active| active.values().filter_map(Value::as_i64).sum())
        .unwrap_or(0)
}

fn signed_payload(object: &Map<String, Value>) -> Map<String, Value> {
    let mut signed = Map::new();
    for key in INSTANCE_SIGNED_KEYS {
        if let Some(value) = object.get(key) {
            signed.insert(key.to_string(), value.clone());
        }
    }
    signed
}

/// Python `json.dumps(..., sort_keys=True)` canonical form.
fn canonical(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => json_string(value),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(canonical).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(object) => {
            let mut keys: Vec<&String> = object.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys
                .iter()
                .map(|key| format!("{}: {}", json_string(key), canonical(&object[*key])))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            character if (character as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => out.push(character),
        }
    }
    out.push('"');
    out
}

/// HMAC-SHA256 (RFC 2104) over the canonical signed payload.
fn hmac_sha256(key: &[u8], message: &[u8]) -> String {
    const BLOCK: usize = 64;
    let mut key_block = [0u8; BLOCK];
    if key.len() > BLOCK {
        key_block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        key_block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    let mut pad = [0u8; BLOCK];
    for (index, byte) in key_block.iter().enumerate() {
        pad[index] = byte ^ 0x36;
    }
    inner.update(pad);
    inner.update(message);
    let inner_digest = inner.finalize();
    let mut outer = Sha256::new();
    for (index, byte) in key_block.iter().enumerate() {
        pad[index] = byte ^ 0x5c;
    }
    outer.update(pad);
    outer.update(inner_digest);
    hex(&outer.finalize())
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn random_instance_id() -> String {
    let mut bytes = [0u8; 16];
    let _ = getrandom::getrandom(&mut bytes);
    hex(&bytes)
}

/// Read the instance key: one bounded secret, trimmed like Python.
pub fn read_instance_secret(path: &std::path::Path) -> std::io::Result<String> {
    let secret = std::fs::read_to_string(path)?.trim().to_string();
    if secret.is_empty() || secret.len() > 128 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "instance key must be 1..128 characters",
        ));
    }
    Ok(secret)
}

#[cfg(unix)]
mod channel {
    use std::fs;
    use std::io::{self, Read, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    use serde_json::Value;

    use super::{InstanceHandler, MAX_MESSAGE};

    /// The ONE AF_UNIX instance-channel listener.
    pub struct InstanceChannel {
        path: PathBuf,
        registration: Option<PathBuf>,
        shutdown: Arc<AtomicBool>,
        handle: Option<thread::JoinHandle<()>>,
    }

    impl InstanceChannel {
        /// Bind the socket, publish the registration, and serve one frame per
        /// accepted connection.
        pub fn start(
            path: PathBuf,
            handler: Arc<InstanceHandler>,
            registration: Option<PathBuf>,
        ) -> io::Result<Self> {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
                fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
            }
            let _ = fs::remove_file(&path);
            let listener = UnixListener::bind(&path)?;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
            listener.set_nonblocking(true)?;
            if let Some(registration) = &registration {
                write_registration(registration, handler.instance_id(), &path)?;
            }
            let shutdown = Arc::new(AtomicBool::new(false));
            let worker_shutdown = Arc::clone(&shutdown);
            let handle = thread::spawn(move || {
                while !worker_shutdown.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((stream, _)) => serve(stream, &handler),
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(_) => break,
                    }
                }
            });
            Ok(Self {
                path,
                registration,
                shutdown,
                handle: Some(handle),
            })
        }

        pub fn stop(&mut self) {
            if self.handle.take().is_some() {
                self.shutdown.store(true, Ordering::SeqCst);
            }
            let _ = fs::remove_file(&self.path);
            if let Some(registration) = &self.registration {
                let _ = fs::remove_file(registration);
            }
        }
    }

    impl Drop for InstanceChannel {
        fn drop(&mut self) {
            self.stop();
        }
    }

    fn serve(mut stream: UnixStream, handler: &InstanceHandler) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));
        let Some(body) = read_frame(&mut stream) else {
            return;
        };
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_secs() as i64)
            .unwrap_or_default();
        let response = handler.handle(&body, now);
        let mut line = serde_json::to_vec(&response).unwrap_or_default();
        line.push(b'\n');
        let _ = stream.write_all(&line);
        let _ = stream.flush();
        let _ = stream.shutdown(std::net::Shutdown::Both);
    }

    fn read_frame(stream: &mut UnixStream) -> Option<Value> {
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut raw = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return None;
            }
            let _ = stream.set_read_timeout(Some(remaining));
            match stream.read(&mut byte) {
                Ok(0) => return None,
                Ok(_) if byte[0] == b'\n' => break,
                Ok(_) => {
                    raw.push(byte[0]);
                    if raw.len() > MAX_MESSAGE {
                        return None;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => return None,
            }
        }
        serde_json::from_slice(&raw).ok()
    }

    fn write_registration(path: &Path, instance_id: &str, pipe_name: &Path) -> io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let payload = serde_json::json!({
            "instance_id": instance_id,
            "pipe_name": pipe_name.display().to_string(),
            "pid": std::process::id(),
            "registered_at": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|value| value.as_secs())
                .unwrap_or_default(),
        });
        let tmp = path.with_extension("tmp");
        fs::write(
            &tmp,
            serde_json::to_vec_pretty(&payload).unwrap_or_default(),
        )?;
        fs::rename(&tmp, path)
    }
}

#[cfg(unix)]
pub use channel::InstanceChannel;

#[cfg(test)]
fn test_gate() -> Arc<ShareGate> {
    let dir = std::env::temp_dir().join(format!(
        "um-gateway-instance-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default()
    ));
    std::fs::create_dir_all(&dir).expect("temp dir");
    let path = dir.join("projection.json");
    std::fs::write(
        &path,
        r#"{"schema_version":2,"config_generation":1,"data":[],"share_max_concurrency":4}"#,
    )
    .expect("projection");
    Arc::new(ShareGate::new(&path, crate::config::AuthMode::Projection).expect("gate"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_hmac_matches_the_rfc_4231_vector() {
        assert_eq!(
            hmac_sha256(b"Jefe", b"what do ya want for nothing?"),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn the_canonical_form_matches_python_sorted_json() {
        let value = json!({"b": 1, "a": ["x", {"d": true, "c": null}]});
        assert_eq!(
            canonical(&value),
            r#"{"a": ["x", {"c": null, "d": true}], "b": 1}"#
        );
    }

    #[test]
    fn a_signed_load_echoes_the_generation_and_terminates_keys() {
        let gate = test_gate();
        let handler = InstanceHandler::new(Arc::clone(&gate), "secret".to_string());
        let body = json!({
            "v": 2,
            "action": "load",
            "request_id": "req-1",
            "ts": 100,
            "instance_id": handler.instance_id(),
            "fields": {"generation": 7, "fingerprint": "abc", "terminate_keys": ["alpha"]},
        });
        let mut signed = signed_payload(body.as_object().expect("object"));
        let mac = hmac_sha256(
            b"secret",
            canonical(&Value::Object(signed.clone())).as_bytes(),
        );
        signed.insert("mac".to_string(), Value::String(mac));
        let response = handler.handle(&Value::Object(signed), 100);
        assert_eq!(response["ok"], json!(true));
        assert_eq!(response["result"]["generation"], json!(7));
        assert_eq!(response["result"]["fingerprint"], json!("abc"));
    }

    #[test]
    fn a_forged_mac_and_a_replayed_request_are_refused() {
        let gate = test_gate();
        let handler = InstanceHandler::new(Arc::clone(&gate), "secret".to_string());
        let body = json!({
            "v": 2,
            "action": "instance-status",
            "request_id": "req-2",
            "ts": 100,
            "instance_id": handler.instance_id(),
            "fields": {},
            "mac": "00",
        });
        assert_eq!(handler.handle(&body, 100)["error_code"], json!("forbidden"));
        let mut signed = signed_payload(body.as_object().expect("object"));
        let mac = hmac_sha256(
            b"secret",
            canonical(&Value::Object(signed.clone())).as_bytes(),
        );
        signed.insert("mac".to_string(), Value::String(mac));
        let signed = Value::Object(signed);
        assert_eq!(handler.handle(&signed, 100)["ok"], json!(true));
        assert_eq!(handler.handle(&signed, 100)["error_code"], json!("replay"));
    }
}

#[cfg(all(test, unix))]
mod channel_tests {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;

    use super::*;

    #[test]
    fn the_channel_answers_a_signed_instance_status() {
        let gate = test_gate();
        let handler = Arc::new(InstanceHandler::new(gate, "secret".to_string()));
        let dir = std::env::temp_dir().join(format!(
            "um-gateway-channel-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|value| value.as_nanos())
                .unwrap_or_default()
        ));
        std::fs::create_dir_all(&dir).expect("temp dir");
        let path = dir.join("instance-sock");
        let mut channel =
            InstanceChannel::start(path.clone(), Arc::clone(&handler), None).expect("start");
        let mut stream = UnixStream::connect(&path).expect("connect");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_secs() as i64)
            .unwrap_or_default();
        let body = json!({
            "v": 2,
            "action": "instance-status",
            "request_id": "req-status",
            "ts": now,
            "instance_id": handler.instance_id(),
            "fields": {},
        });
        let mut signed = signed_payload(body.as_object().expect("object"));
        let mac = hmac_sha256(
            b"secret",
            canonical(&Value::Object(signed.clone())).as_bytes(),
        );
        signed.insert("mac".to_string(), Value::String(mac));
        let mut line = serde_json::to_vec(&Value::Object(signed)).expect("encode");
        line.push(b'\n');
        stream.write_all(&line).expect("write");
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).expect("read");
        let response: Value = serde_json::from_slice(&raw).expect("response");
        assert_eq!(response["ok"], json!(true));
        assert_eq!(
            response["result"]["instance_id"],
            json!(handler.instance_id())
        );
        channel.stop();
        let _ = std::fs::remove_dir_all(dir);
    }
}
