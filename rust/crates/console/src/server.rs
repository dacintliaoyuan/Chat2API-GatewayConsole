//! Candidate 8002 console HTTP server.
//!
//! The candidate binds only an ephemeral loopback port. It serves the S5a
//! read-only core from `usagesrv.py`: `/health`, `/api/summary`,
//! `/api/console/services`, `/api/console/health`, `/api/console/actions`
//! and `/api/console/operations`. The cloud role refuses the non-UM read
//! faces with `503 capability_not_assembled` before reading or probing
//! anything.

use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use um_gateway::http::{read_request_with_body_cap, write_response, Headers, Request, Response};

use crate::actions::action_catalogue;
use crate::body::{
    read_console_body, reject_duplicate_sensitive_header, reject_unknown_fields,
    CONSOLE_MAX_REQUEST_BYTES,
};
use crate::config::{ConfigError, ConsoleConfig, Role};
use crate::error::ConsoleError;
use crate::operations::OperationsReader;
use crate::runtime::{LinuxPidFileFacts, RuntimeFacts, RuntimeSnapshot};
use crate::share_read::{ShareReadSeams, ShareReadStore, UnwiredSeams};
use crate::summary::SummaryCache;

const IDLE_CONNECTION_TIMEOUT: Duration = Duration::from_secs(30);
const CONSOLE_STATUS_TTL: Duration = Duration::from_secs(2);
const CLOUD_CAPABILITY_PREFIXES: [&str; 9] = [
    "usage-tokens",
    "usage-rates",
    "gateway-connection",
    "logs",
    "log-sources",
    "log-entries",
    "log-overview",
    "stats",
    "um",
];

pub struct ConsoleServer;

pub struct AppState {
    pub config: ConsoleConfig,
    pub runtime: Arc<dyn RuntimeFacts>,
    pub summary: SummaryCache,
    pub operations: OperationsReader,
    pub share: ShareReadStore,
    pub share_seams: Arc<dyn ShareReadSeams>,
    services_cache: Mutex<Option<(Value, Instant)>>,
}

pub struct ServerHandle {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    state: Arc<AppState>,
}

impl ConsoleServer {
    pub fn start(config: ConsoleConfig) -> Result<ServerHandle, ServerError> {
        config.validate_candidate().map_err(ServerError::Config)?;
        let runtime = Arc::new(LinuxPidFileFacts::from_config(&config));
        Self::start_with_runtime(config, runtime)
    }

    pub fn start_with_runtime(
        config: ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
    ) -> Result<ServerHandle, ServerError> {
        Self::start_with_seams(config, runtime, Arc::new(UnwiredSeams))
    }

    pub fn start_with_seams(
        config: ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
        share_seams: Arc<dyn ShareReadSeams>,
    ) -> Result<ServerHandle, ServerError> {
        config.validate_candidate().map_err(ServerError::Config)?;
        let state = Arc::new(AppState {
            operations: OperationsReader::new(config.console_state_file.clone()),
            share: ShareReadStore::with_options(
                config.share_store_file.clone().unwrap_or_default(),
                config.public_origin.clone(),
                config.share_secret_key_file.clone(),
            ),
            share_seams,
            config: config.clone(),
            runtime,
            summary: SummaryCache::new(),
            services_cache: Mutex::new(None),
        });
        let listener = TcpListener::bind((config.bind_host.as_str(), config.bind_port))
            .map_err(ServerError::Io)?;
        listener.set_nonblocking(true).map_err(ServerError::Io)?;
        let addr = listener.local_addr().map_err(ServerError::Io)?;
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_shutdown = Arc::clone(&shutdown);
        let worker_state = Arc::clone(&state);
        let handle = thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let connection_state = Arc::clone(&worker_state);
                        thread::spawn(move || {
                            handle_connection(connection_state, stream);
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(ServerHandle {
            addr,
            shutdown,
            handle: Some(handle),
            state,
        })
    }
}

impl std::fmt::Debug for ServerHandle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ServerHandle")
            .field("addr", &self.addr)
            .finish_non_exhaustive()
    }
}

impl ServerHandle {
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn state(&self) -> Arc<AppState> {
        Arc::clone(&self.state)
    }

    pub fn shutdown(&mut self) {
        if let Some(handle) = self.handle.take() {
            self.shutdown.store(true, Ordering::SeqCst);
            let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
            let _ = handle.join();
        }
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[derive(Debug)]
pub enum ServerError {
    Config(ConfigError),
    Io(std::io::Error),
}

impl std::fmt::Display for ServerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Config(error) => write!(formatter, "{error}"),
            Self::Io(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ServerError {}

fn handle_connection(state: Arc<AppState>, mut stream: TcpStream) {
    let _ = stream.set_read_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    loop {
        let request = match read_request_with_body_cap(
            &mut stream,
            IpAddr::V4(std::net::Ipv4Addr::LOCALHOST),
            CONSOLE_MAX_REQUEST_BYTES,
        ) {
            Ok(Some(request)) => request,
            Ok(None) => return,
            Err(_) => {
                let response = ConsoleError::body(400, "invalid_request").response();
                let _ = write_response(&mut stream, &response);
                return;
            }
        };
        let response = dispatch(&state, &request);
        let close = response.close;
        if write_response(&mut stream, &response).is_err() || close {
            return;
        }
    }
}

fn dispatch(state: &AppState, request: &Request) -> Response {
    if !host_allowed(request.headers.get("Host").unwrap_or_default())
        || !origin_allowed(request.headers.get("Origin"), state.config.port)
    {
        return ConsoleError::local_only().response();
    }
    let target = rewrite_alias(request.raw_target());
    let path = target.split('?').next().unwrap_or(&target);
    let method = request.method.as_str();

    if path == "/health" {
        if method == "GET" {
            return send_json(
                200,
                &json!({
                    "status": "ok",
                    "service": "usagesrv",
                    "port": state.config.port,
                }),
            );
        }
        return ConsoleError::unknown_path().response();
    }
    if path == "/api/summary" {
        if method == "GET" {
            return send_json(200, &state.summary.payload());
        }
        return ConsoleError::unknown_path().response();
    }
    if !path.starts_with("/api/console/") {
        return ConsoleError::unknown_path().response();
    }
    if let Err(error) = reject_duplicate_sensitive_header(&request.headers) {
        return error.response();
    }
    let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    let rest: Vec<&str> = segments.iter().skip(2).copied().collect();
    if state.config.role == Role::Cloud {
        if let Some(blocked) = rest
            .first()
            .filter(|prefix| CLOUD_CAPABILITY_PREFIXES.contains(prefix))
        {
            return ConsoleError::capability_not_assembled(blocked).response();
        }
    }
    match method {
        "GET" => handle_console_get(state, &rest, &target),
        "POST" | "PATCH" | "DELETE" => handle_console_write(state, method, &rest, request),
        _ => ConsoleError::unknown_path().response(),
    }
}

fn handle_console_get(state: &AppState, rest: &[&str], raw_target: &str) -> Response {
    match rest {
        ["services"] => services_response(state),
        ["health"] => health_response(state),
        ["actions"] => send_json(200, &action_catalogue()),
        ["share-management", "status"] => match state.share.status(&*state.share_seams) {
            Ok(payload) => send_json(200, &payload),
            Err(error) => error.response(),
        },
        ["share-management", "keys"] => match state.share.keys() {
            Ok(payload) => send_json(200, &payload),
            Err(error) => error.response(),
        },
        ["share-management", "keys", key_id, "secret"] => {
            let expected = query_value(raw_target, "expected_row_version");
            match state.share.secret(key_id, expected.as_deref()) {
                Ok((status, payload)) => send_json(status, &payload),
                Err(error) => error.response(),
            }
        }
        ["operations"] => match state.operations.list(raw_target) {
            Ok(payload) => send_json(200, &payload),
            Err(error) => error.response(),
        },
        ["operations", operation_id] => match state.operations.get(operation_id) {
            Ok(payload) => send_json(200, &payload),
            Err(error) => error.response(),
        },
        _ => ConsoleError::unknown_path().response(),
    }
}

fn handle_console_write(
    state: &AppState,
    method: &str,
    rest: &[&str],
    request: &Request,
) -> Response {
    let body = match read_console_body(request) {
        Ok(body) => body,
        Err(error) => return error.response(),
    };
    if method == "POST" && rest == ["operations"] {
        if let Err(error) =
            reject_unknown_fields(&body, &["action", "target", "params", "idempotency_key"])
        {
            return error.response();
        }
        // S6 owns operation submission; S5a only owns the read face and
        // its request boundary.
        return ConsoleError::not_implemented("operations_submit_s6").response();
    }
    let _ = state;
    ConsoleError::unknown_path().response()
}

fn services_response(state: &AppState) -> Response {
    let now = Instant::now();
    if let Ok(cache) = state.services_cache.lock() {
        if let Some((payload, at)) = cache.as_ref() {
            if now.duration_since(*at) < CONSOLE_STATUS_TTL {
                let mut payload = payload.clone();
                if let Some(meta) = payload.get_mut("meta").and_then(Value::as_object_mut) {
                    meta.insert("cached".to_string(), json!(true));
                    meta.insert(
                        "cache_age_seconds".to_string(),
                        json!(round_one_decimal(now.duration_since(*at).as_secs_f64())),
                    );
                }
                return send_json(200, &payload);
            }
        }
    }
    let snapshot = match state.runtime.snapshot() {
        Ok(snapshot) => snapshot,
        Err(_) => return ConsoleError::runtime_unavailable().response(),
    };
    let payload = services_payload(state, &snapshot);
    if let Ok(mut cache) = state.services_cache.lock() {
        *cache = Some((payload.clone(), Instant::now()));
    }
    let mut payload = payload;
    if let Some(meta) = payload.get_mut("meta").and_then(Value::as_object_mut) {
        meta.insert("cached".to_string(), json!(false));
    }
    send_json(200, &payload)
}

fn health_response(state: &AppState) -> Response {
    let snapshot = match state.runtime.snapshot() {
        Ok(snapshot) => snapshot,
        Err(_) => return ConsoleError::runtime_unavailable().response(),
    };
    let mut services = vec![service_row(&snapshot, state.config.gateway_port)];
    services.push(json!({
        "id": "usagesrv",
        "port": state.config.port,
        "state": "listening",
        "note": "console host itself",
    }));
    send_json(
        200,
        &json!({
            "checked_at": epoch_seconds(),
            "cached": false,
            "services": services,
        }),
    )
}

fn services_payload(state: &AppState, snapshot: &RuntimeSnapshot) -> Value {
    json!({
        "ok": true,
        "services": [service_row(snapshot, state.config.gateway_port)],
        "meta": {
            "source": "linux-pid-file",
            "generated_epoch": epoch_seconds(),
        },
    })
}

fn service_row(snapshot: &RuntimeSnapshot, gateway_port: u16) -> Value {
    json!({
        "key": "share-gateway",
        "display_name": "UM 分享入口 8100",
        "port": gateway_port,
        "state": snapshot.display_state(),
        "healthy": snapshot.healthy,
        "detail": snapshot.detail,
        "process_ids": snapshot.process_ids,
    })
}

fn send_json(status: u16, payload: &Value) -> Response {
    Response::json(status, payload).with_header("Cache-Control", "no-store")
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn round_one_decimal(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn rewrite_alias(target: &str) -> String {
    let (path, query) = match target.split_once('?') {
        Some((path, query)) => (path, Some(query)),
        None => (target, None),
    };
    let rewritten = if path == "/gw/usagesrv" {
        "/".to_string()
    } else if let Some(rest) = path.strip_prefix("/gw/usagesrv/") {
        format!("/{rest}")
    } else {
        path.to_string()
    };
    match query {
        Some(query) => format!("{rewritten}?{query}"),
        None => rewritten,
    }
}

/// `usagesrv.py::host_allowed`: the Host header must name a loopback host;
/// a malformed port refuses instead of being ignored.
fn host_allowed(host_header: &str) -> bool {
    let host = host_header.trim();
    if let Some(rest) = host.strip_prefix('[') {
        let Some((inside, tail)) = rest.split_once(']') else {
            return false;
        };
        if !matches!(tail, "" | ":") && !tail.starts_with(':') {
            return false;
        }
        if let Some(port) = tail.strip_prefix(':') {
            if port.is_empty() || port.parse::<u16>().is_err() {
                return false;
            }
        }
        return inside.eq_ignore_ascii_case("::1");
    }
    let (name, port) = match host.rsplit_once(':') {
        Some((name, port)) => (name, Some(port)),
        None => (host, None),
    };
    if name.contains(':') {
        return false;
    }
    if let Some(port) = port {
        if port.is_empty() || port.parse::<u16>().is_err() {
            return false;
        }
    }
    matches!(
        name.to_ascii_lowercase().as_str(),
        "127.0.0.1" | "localhost"
    )
}

/// `usagesrv.py::origin_allowed`: missing Origin is accepted; otherwise the
/// origin must be `http`, loopback, and this console's configured port.
fn origin_allowed(origin: Option<&str>, port: u16) -> bool {
    let Some(origin) = origin else {
        return true;
    };
    let lower = origin.to_ascii_lowercase();
    let Some(rest) = lower.strip_prefix("http://") else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or(rest);
    let authority = authority
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(authority);
    let (name, port_text) = match authority.rsplit_once(':') {
        Some((name, port)) => (name, Some(port)),
        None => (authority, None),
    };
    if name.contains(':') {
        return false;
    }
    let Some(port_text) = port_text else {
        return false;
    };
    if port_text.is_empty() || !port_text.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    matches!(name, "127.0.0.1" | "localhost") && port_text.parse::<u16>().ok() == Some(port)
}

/// Test seam: the Host/Origin gate.
pub fn request_origin_allowed(headers: &Headers, port: u16) -> bool {
    host_allowed(headers.get("Host").unwrap_or_default())
        && origin_allowed(headers.get("Origin"), port)
}

/// Test seam: the cloud capability gate prefix set.
pub fn cloud_capability_prefixes() -> &'static [&'static str; 9] {
    &CLOUD_CAPABILITY_PREFIXES
}

/// Test seam: one rewritten alias target.
pub fn alias_target(target: &str) -> String {
    rewrite_alias(target)
}

/// Test seam: the Python `_view` field map for one operation row.
pub fn operation_view_for_tests(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    crate::operations::operation_view_for_tests(row)
}

/// Test seam: read one body through the console boundary.
pub fn read_body_for_tests(request: &Request) -> Result<Map<String, Value>, ConsoleError> {
    read_console_body(request)
}

/// One query-string value, Python `parse_qs` default semantics: the first
/// value wins, a blank value is absent, and `+`/`%XX` are decoded.
fn query_value(raw_target: &str, name: &str) -> Option<String> {
    let query = raw_target.split_once('?')?.1;
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if decode_query_part(key) != name {
            continue;
        }
        let value = decode_query_part(value);
        return if value.is_empty() { None } else { Some(value) };
    }
    None
}

fn decode_query_part(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => output.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                let high = (bytes[index + 1] as char).to_digit(16);
                let low = (bytes[index + 2] as char).to_digit(16);
                match (high, low) {
                    (Some(high), Some(low)) => {
                        output.push((high * 16 + low) as u8);
                        index += 2;
                    }
                    _ => output.push(b'%'),
                }
            }
            byte => output.push(byte),
        }
        index += 1;
    }
    String::from_utf8_lossy(&output).to_string()
}
