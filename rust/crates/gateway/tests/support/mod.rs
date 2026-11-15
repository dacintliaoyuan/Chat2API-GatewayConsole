#![allow(dead_code)]

use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::config::{AuthMode, GatewayConfig};
use um_gateway::GatewayServer;

pub const STUB_TOKEN: &str = "fixture-upstream-token";
pub const SHARE_SECRET: &str = "secret";
pub const SECOND_SECRET: &str = "second-secret";
pub const THIRD_SECRET: &str = "third-secret";
pub const CONTROL_TOKEN: &str = "fixture-control-token";
pub const ALLOWED_ORIGIN: &str = "https://allowed.example";
pub const DENIED_ORIGIN: &str = "https://denied.example";

pub fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/rust_gateway_rev1.json")
}

pub fn fixture() -> Value {
    serde_json::from_str(
        &std::fs::read_to_string(fixture_path()).expect("read gateway fixture"),
    )
    .expect("parse gateway fixture")
}

pub fn shape(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = serde_json::Map::new();
            for (key, value) in map {
                output.insert(key.clone(), shape(value));
            }
            json!({"object": output})
        }
        Value::Array(items) => {
            if items.is_empty() {
                json!({"array": []})
            } else {
                json!({"array": [shape(&items[0])]})
            }
        }
        Value::Bool(_) => json!("bool"),
        Value::Number(_) => json!("number"),
        Value::String(_) => json!("string"),
        Value::Null => json!("null"),
    }
}

pub fn sha256_hex(value: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(value.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn legacy_share_config() -> String {
    json!({"clients": {
        "friend": sha256_hex(SHARE_SECRET),
        "second": sha256_hex(SECOND_SECRET),
        "third": sha256_hex(THIRD_SECRET),
    }})
    .to_string()
}

pub fn projection_share_config() -> String {
    json!({
        "schema_version": 2,
        "config_generation": 7,
        "share_max_concurrency": 1,
        "data": [
            {"key_id": "friend", "name": "friend",
             "secret_hash": sha256_hex(SHARE_SECRET),
             "secret_version": 3, "enabled": true, "max_concurrency": 1},
            {"key_id": "second", "name": "second",
             "secret_hash": sha256_hex(SECOND_SECRET),
             "secret_version": 1, "enabled": true, "max_concurrency": 1},
            {"key_id": "third", "name": "third",
             "secret_hash": sha256_hex(THIRD_SECRET),
             "secret_version": 1, "enabled": true, "max_concurrency": 1},
        ],
    })
    .to_string()
}

pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(prefix: &str) -> Self {
        static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
        let root = std::env::temp_dir();
        for _ in 0..100 {
            let sequence = SEQUENCE.fetch_add(1, Ordering::SeqCst);
            let path = root.join(format!(
                "{prefix}{}-{sequence}",
                std::process::id()
            ));
            match std::fs::create_dir(&path) {
                Ok(()) => return Self { path },
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("cannot create temp dir: {error}"),
            }
        }
        panic!("cannot allocate a temp dir");
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    pub fn write(&self, name: &str, contents: &str) -> PathBuf {
        let path = self.path.join(name);
        std::fs::write(&path, contents).expect("write temp file");
        path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

#[derive(Debug, Clone)]
pub struct StubBehaviour {
    pub delay: Duration,
    pub models_status: u16,
    pub models_body: Option<Value>,
    pub version_status: u16,
    pub version_body: Option<Value>,
}

impl Default for StubBehaviour {
    fn default() -> Self {
        Self {
            delay: Duration::ZERO,
            models_status: 200,
            models_body: None,
            version_status: 200,
            version_body: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SeenRequest {
    pub path: String,
    pub authorization: Option<String>,
    pub models_header: Option<String>,
}

pub struct StubUpstream {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    behaviour: Arc<Mutex<StubBehaviour>>,
    seen: Arc<Mutex<Vec<SeenRequest>>>,
}

impl StubUpstream {
    pub fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind stub upstream");
        listener.set_nonblocking(true).expect("nonblocking stub");
        let addr = listener.local_addr().expect("stub addr");
        let shutdown = Arc::new(AtomicBool::new(false));
        let behaviour = Arc::new(Mutex::new(StubBehaviour {
            delay: Duration::from_millis(0),
            models_status: 200,
            models_body: Some(json!({
                "object": "list",
                "data": [
                    {"id": "fixture-model-a", "name": "Fixture A"},
                    {"id": "fixture-model-b", "name": "Fixture B"},
                ],
            })),
            version_status: 200,
            version_body: Some(json!({"version": "0.7.0-fixture"})),
        }));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let worker_shutdown = Arc::clone(&shutdown);
        let worker_behaviour = Arc::clone(&behaviour);
        let worker_seen = Arc::clone(&seen);
        let handle = thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let behaviour = Arc::clone(&worker_behaviour);
                        let seen = Arc::clone(&worker_seen);
                        thread::spawn(move || {
                            let _ = serve_stub(stream, &behaviour, &seen);
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            shutdown,
            handle: Some(handle),
            behaviour,
            seen,
        }
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn set(&self, behaviour: StubBehaviour) {
        *self.behaviour.lock().expect("stub behaviour") = behaviour;
    }

    pub fn seen(&self) -> Vec<SeenRequest> {
        self.seen.lock().expect("stub seen").clone()
    }
}

impl Drop for StubUpstream {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            self.shutdown.store(true, Ordering::SeqCst);
            let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
            let _ = handle.join();
        }
    }
}

fn serve_stub(
    mut stream: TcpStream,
    behaviour: &Mutex<StubBehaviour>,
    seen: &Mutex<Vec<SeenRequest>>,
) -> std::io::Result<()> {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let request = match read_client_request(&mut stream)? {
        Some(request) => request,
        None => return Ok(()),
    };
    let path = request.path.split('?').next().unwrap_or_default().to_string();
    let authorization = request.header("authorization");
    let models_header = request.header("x-chat2api-models");
    seen.lock().expect("stub seen").push(SeenRequest {
        path: path.clone(),
        authorization: authorization.clone(),
        models_header,
    });
    let behaviour = behaviour.lock().expect("stub behaviour").clone();
    let (status, payload) = if path == "/api/config" {
        (
            200,
            json!({
                "name": "Open WebUI",
                "version": "0.7.0-fixture",
                "features": {"enable_api_keys": false},
                "oauth": {"providers": {"fixture-sso": {}}},
            }),
        )
    } else if path == "/api/version" {
        (
            behaviour.version_status,
            behaviour
                .version_body
                .clone()
                .unwrap_or_else(|| json!({"version": "0.7.0-fixture"})),
        )
    } else if path == "/api/models" {
        let body = match authorization.as_deref() {
            Some(value) if value.contains("rejected-token") => {
                (401, json!({"detail": "rejected"}))
            }
            Some(value) if value.contains("error-token") => {
                (500, json!({"detail": "upstream failed"}))
            }
            Some(value) if value.contains("invalid-shape-token") => {
                (200, json!({"not": "a catalog"}))
            }
            _ => (
                behaviour.models_status,
                behaviour
                    .models_body
                    .clone()
                    .unwrap_or_else(|| json!({"object": "list", "data": []})),
            ),
        };
        if behaviour.delay > Duration::ZERO
            && !authorization
                .as_deref()
                .is_some_and(|value| value.contains("rejected-token") || value.contains("error-token"))
        {
            thread::sleep(behaviour.delay);
        }
        body
    } else {
        (404, json!({"detail": "not found"}))
    };
    let raw = serde_json::to_vec(&payload).expect("stub json");
    let head = format!(
        "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        if status == 200 { "OK" } else { "Error" },
        raw.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(&raw)?;
    stream.flush()
}

pub struct ClientRequest {
    pub method: String,
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
}

impl ClientRequest {
    pub fn get(target: &str) -> Self {
        Self {
            method: "GET".to_string(),
            target: target.to_string(),
            headers: Vec::new(),
            body: None,
        }
    }

    pub fn options(target: &str) -> Self {
        Self {
            method: "OPTIONS".to_string(),
            target: target.to_string(),
            headers: Vec::new(),
            body: None,
        }
    }

    pub fn post(target: &str, body: &str) -> Self {
        Self {
            method: "POST".to_string(),
            target: target.to_string(),
            headers: Vec::new(),
            body: Some(body.as_bytes().to_vec()),
        }
    }

    pub fn bearer(mut self, secret: &str) -> Self {
        self.headers.push((
            "Authorization".to_string(),
            format!("Bearer {secret}"),
        ));
        self
    }

    pub fn header(mut self, name: &str, value: &str) -> Self {
        self.headers.push((name.to_string(), value.to_string()));
        self
    }

    pub fn json_body(mut self, body: &str) -> Self {
        self.headers
            .push(("Content-Type".to_string(), "application/json".to_string()));
        self.body = Some(body.as_bytes().to_vec());
        self
    }
}

pub struct ClientResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl ClientResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or(Value::Null)
    }
}

pub fn client_request(
    addr: SocketAddr,
    request: &ClientRequest,
) -> std::io::Result<ClientResponse> {
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let mut raw = format!("{} {} HTTP/1.1\r\n", request.method, request.target);
    let has_host = request
        .headers
        .iter()
        .any(|(name, _)| name.eq_ignore_ascii_case("host"));
    if !has_host {
        raw.push_str(&format!("Host: {addr}\r\n"));
    }
    for (name, value) in &request.headers {
        raw.push_str(&format!("{name}: {value}\r\n"));
    }
    if let Some(body) = &request.body {
        let has_length = request
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("content-length"));
        if !has_length {
            raw.push_str(&format!("Content-Length: {}\r\n", body.len()));
        }
    }
    raw.push_str("\r\n");
    stream.write_all(raw.as_bytes())?;
    if let Some(body) = &request.body {
        stream.write_all(body)?;
    }
    stream.flush()?;
    read_client_response(&mut stream)
}

fn read_client_request(stream: &mut TcpStream) -> std::io::Result<Option<RawRequest>> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            return Ok(None);
        }
        raw.extend_from_slice(&buffer[..read]);
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path = parts.next().unwrap_or_default().to_string();
    let mut headers = Vec::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }
    let length = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, value)| value.parse::<usize>().ok())
        .unwrap_or(0);
    let mut body = raw[header_end..].to_vec();
    while body.len() < length {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        body.extend_from_slice(&buffer[..read]);
    }
    Ok(Some(RawRequest {
        method,
        path,
        headers,
        body,
    }))
}

struct RawRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl RawRequest {
    fn header(&self, name: &str) -> Option<String> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.clone())
    }
}

fn read_client_response(stream: &mut TcpStream) -> std::io::Result<ClientResponse> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "connection closed before the response head",
            ));
        }
        raw.extend_from_slice(&buffer[..read]);
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid status line")
        })?;
    let mut headers = Vec::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }
    let length = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, value)| value.parse::<usize>().ok());
    let mut body = raw[header_end..].to_vec();
    if let Some(length) = length {
        while body.len() < length {
            let read = stream.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            body.extend_from_slice(&buffer[..read]);
        }
        body.truncate(length);
    } else {
        loop {
            let read = stream.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            body.extend_from_slice(&buffer[..read]);
        }
    }
    Ok(ClientResponse {
        status,
        headers,
        body,
    })
}

pub struct Gateway {
    pub server: GatewayServerHandle,
    pub addr: SocketAddr,
}

#[derive(Debug)]
pub struct GatewayServerHandle {
    inner: um_gateway::ServerHandle,
}

impl GatewayServerHandle {
    pub fn addr(&self) -> SocketAddr {
        self.inner.addr()
    }
}

impl Drop for GatewayServerHandle {
    fn drop(&mut self) {
        self.inner.shutdown();
    }
}

pub fn start_gateway(
    stub: &StubUpstream,
    temp: &TempDir,
    share: bool,
) -> Gateway {
    let share_config = if share {
        Some(temp.write("share.json", &legacy_share_config()))
    } else {
        None
    };
    let control = temp.write("control.token", CONTROL_TOKEN);
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config,
        control_token_file: Some(control),
        allowed_origins: [ALLOWED_ORIGIN.to_string()].into_iter().collect(),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let server = GatewayServer::start(config).expect("start candidate gateway");
    let addr = server.addr();
    Gateway {
        server: GatewayServerHandle { inner: server },
        addr,
    }
}

pub fn start_projection_gateway(
    stub: &StubUpstream,
    temp: &TempDir,
) -> Gateway {
    let share_config = Some(temp.write("share-projection.json", &projection_share_config()));
    let control = temp.write("control.token", CONTROL_TOKEN);
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config,
        control_token_file: Some(control),
        allowed_origins: [ALLOWED_ORIGIN.to_string()].into_iter().collect(),
        auth_mode: AuthMode::Projection,
        ..GatewayConfig::default()
    };
    let server = GatewayServer::start(config).expect("start projection gateway");
    let addr = server.addr();
    Gateway {
        server: GatewayServerHandle { inner: server },
        addr,
    }
}

pub fn wait_until<F>(timeout: Duration, mut predicate: F)
where
    F: FnMut() -> bool,
{
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if predicate() {
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert!(predicate(), "condition did not become true in {timeout:?}");
}

pub fn unique_temp_prefix() -> &'static str {
    "um-rust-gateway-test-"
}

pub fn production_ports() -> BTreeSet<u16> {
    [5173, 8000, 8001, 8002, 8003, 8097, 8099]
        .into_iter()
        .collect()
}

pub fn error_body(response: &ClientResponse) -> Value {
    response.json()
}

pub fn body_field<'a>(body: &'a Value, field: &str) -> &'a Value {
    body.get("error")
        .and_then(|error| error.get(field))
        .unwrap_or(&Value::Null)
}
