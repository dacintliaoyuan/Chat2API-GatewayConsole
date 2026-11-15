#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use um_console::config::{ConsoleConfig, EntryKind};
use um_console::runtime::{RuntimeFacts, RuntimeSnapshot, StaticRuntimeFacts};
use um_console::ConsoleServer;
use um_test_harness::TempDataDir;

pub const CONTROL_TOKEN: &str = "fixture-control-token";

pub fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/rust_console_rev1.json")
}

pub fn fixture() -> Value {
    serde_json::from_str(&std::fs::read_to_string(fixture_path()).expect("read console fixture"))
        .expect("parse console fixture")
}

pub fn shape(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = serde_json::Map::new();
            for (key, value) in map {
                output.insert(key.clone(), shape(value));
            }
            serde_json::json!({"object": output})
        }
        Value::Array(items) => {
            if items.is_empty() {
                serde_json::json!({"array": []})
            } else {
                serde_json::json!({"array": [shape(&items[0])]})
            }
        }
        Value::Bool(_) => serde_json::json!("bool"),
        Value::Number(_) => serde_json::json!("number"),
        Value::String(_) => serde_json::json!("string"),
        Value::Null => serde_json::json!("null"),
    }
}

pub fn stopped_runtime() -> Arc<dyn RuntimeFacts> {
    Arc::new(StaticRuntimeFacts::new(RuntimeSnapshot::stopped(
        "pid file absent",
    )))
}

pub fn temp() -> TempDataDir {
    TempDataDir::new("um-rust-console-test-").expect("temp data dir")
}

pub fn base_config(temp: &TempDataDir) -> ConsoleConfig {
    ConsoleConfig {
        bind_host: "127.0.0.1".to_string(),
        bind_port: 0,
        port: 8002,
        gateway_port: 8003,
        console_state_file: Some(temp.path().join("console.sqlite3")),
        gateway_pid_file: temp.path().join("share-gateway.pid"),
        gateway_state_dir: temp.path().join("run"),
        proc_root: temp.path().join("proc"),
        gateway_entry: "chat2api.py".to_string(),
        gateway_entry_kind: EntryKind::Python,
        ..ConsoleConfig::default()
    }
}

pub struct Console {
    server: um_console::ServerHandle,
    pub addr: SocketAddr,
}

impl Console {
    pub fn state(&self) -> Arc<um_console::AppState> {
        self.server.state()
    }
}

impl Drop for Console {
    fn drop(&mut self) {
        self.server.shutdown();
    }
}

pub fn start_console_with(
    temp: &TempDataDir,
    runtime: Arc<dyn RuntimeFacts>,
    configure: impl FnOnce(&mut ConsoleConfig),
) -> Console {
    let mut config = base_config(temp);
    configure(&mut config);
    let server =
        ConsoleServer::start_with_runtime(config, runtime).expect("start candidate console");
    let addr = server.addr();
    Console { server, addr }
}

pub fn start_console(temp: &TempDataDir) -> Console {
    start_console_with(temp, stopped_runtime(), |_| {})
}

#[derive(Debug, Clone)]
pub struct ClientRequest {
    pub method: String,
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub auto_content_length: bool,
}

impl ClientRequest {
    pub fn get(target: &str) -> Self {
        Self {
            method: "GET".to_string(),
            target: target.to_string(),
            headers: Vec::new(),
            body: None,
            auto_content_length: true,
        }
    }

    pub fn post(target: &str, body: impl Into<Vec<u8>>) -> Self {
        Self {
            method: "POST".to_string(),
            target: target.to_string(),
            headers: Vec::new(),
            body: Some(body.into()),
            auto_content_length: true,
        }
    }

    pub fn header(mut self, name: &str, value: &str) -> Self {
        self.headers.push((name.to_string(), value.to_string()));
        self
    }

    pub fn json_body(mut self, body: impl Into<Vec<u8>>) -> Self {
        self.headers
            .push(("Content-Type".to_string(), "application/json".to_string()));
        self.body = Some(body.into());
        self
    }

    pub fn without_content_length(mut self) -> Self {
        self.auto_content_length = false;
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
    let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));
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
        if request.auto_content_length && !has_length {
            raw.push_str(&format!("Content-Length: {}\r\n", body.len()));
        }
    }
    raw.push_str("\r\n");
    stream.write_all(raw.as_bytes())?;
    if let Some(body) = &request.body {
        stream.write_all(body)?;
    }
    stream.flush()?;
    read_response(&mut stream)
}

fn read_response(stream: &mut TcpStream) -> std::io::Result<ClientResponse> {
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

pub fn production_ports() -> [u16; 7] {
    [5173, 8000, 8001, 8002, 8003, 8097, 8099]
}

pub fn write_proc_cmdline(root: &std::path::Path, pid: u32, argv: &[&str]) {
    let directory = root.join(pid.to_string());
    std::fs::create_dir_all(&directory).expect("create proc dir");
    let mut bytes = Vec::new();
    for part in argv {
        bytes.extend_from_slice(part.as_bytes());
        bytes.push(0);
    }
    std::fs::write(directory.join("cmdline"), bytes).expect("write cmdline");
}
