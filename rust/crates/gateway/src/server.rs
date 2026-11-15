//! Candidate gateway HTTP router.
//!
//! The candidate binds only a loopback ephemeral port (`127.0.0.1:0` by default)
//! and refuses a governed production port before the bind. It never starts
//! `chat2api.py`, the broker or the display relay, and it never reads or writes
//! production data.

use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Value};

use crate::body::{
    read_json_body, reject_unknown_fields, BodyPolicy,
};
use crate::config::{AuthMode, ConfigError, GatewayConfig};
use crate::error::GatewayError;
use crate::gate::{
    constant_time_eq, read_control_token, InflightCtx, RefusalReason, ShareGate,
};
use crate::http::{
    read_request, write_response, BodyRead, Headers, Request, Response,
};
use crate::readonly::ProxyState;

const IDLE_CONNECTION_TIMEOUT: Duration = Duration::from_secs(30);

/// A running candidate gateway.
pub struct GatewayServer;

/// The state shared by every accepted connection.
pub struct AppState {
    pub config: GatewayConfig,
    pub proxy: ProxyState,
    pub gate: Option<Arc<ShareGate>>,
    pub control_token: Option<String>,
    pub local_api_key: String,
    pub require_local_api_key: bool,
}

/// A started candidate server.
pub struct ServerHandle {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    state: Arc<AppState>,
}

impl GatewayServer {
    /// Start a candidate server; the socket is bound before the thread starts.
    pub fn start(config: GatewayConfig) -> Result<ServerHandle, ServerError> {
        config.validate_candidate().map_err(ServerError::Config)?;
        let gate = match &config.share_config {
            Some(path) => Some(Arc::new(
                ShareGate::new(path, config.auth_mode).map_err(ServerError::Config)?,
            )),
            None => None,
        };
        let control_token = config
            .control_token_file
            .as_ref()
            .and_then(|path| read_control_token(path));
        let proxy = ProxyState::new(&config).map_err(ServerError::Config)?;
        proxy.probe_config();
        let local_api_key = config.local_api_key.clone();
        let require_local_api_key = !local_api_key.is_empty();
        let state = Arc::new(AppState {
            config: config.clone(),
            proxy,
            gate,
            control_token,
            local_api_key,
            require_local_api_key,
        });
        let listener = TcpListener::bind((config.bind_host.as_str(), config.bind_port))
            .map_err(ServerError::Io)?;
        listener
            .set_nonblocking(true)
            .map_err(ServerError::Io)?;
        let addr = listener.local_addr().map_err(ServerError::Io)?;
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_shutdown = Arc::clone(&shutdown);
        let worker_state = Arc::clone(&state);
        let handle = thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, peer)) => {
                        let connection_state = Arc::clone(&worker_state);
                        thread::spawn(move || {
                            handle_connection(connection_state, stream, peer.ip());
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

struct Admission {
    key_id: String,
    ctx: Arc<InflightCtx>,
}

fn handle_connection(state: Arc<AppState>, mut stream: TcpStream, peer_ip: IpAddr) {
    let _ = stream.set_read_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    loop {
        let request = match read_request(&mut stream, peer_ip) {
            Ok(Some(request)) => request,
            Ok(None) => return,
            Err(_) => {
                let response = GatewayError::invalid_request(
                    "Invalid request line or headers",
                )
                .response();
                let _ = write_response(&mut stream, &response);
                return;
            }
        };
        let (response, admission) = dispatch(&state, &request, &stream);
        let close = response.close;
        let write_ok = write_response(&mut stream, &response).is_ok();
        release_admission(&state, admission);
        if close || !write_ok {
            return;
        }
    }
}

fn release_admission(state: &AppState, admission: Option<Admission>) {
    let Some(admission) = admission else {
        return;
    };
    if let Some(gate) = &state.gate {
        gate.unregister_inflight(&admission.key_id, &admission.ctx);
        gate.release(&admission.key_id);
    }
}

fn dispatch(
    state: &AppState,
    request: &Request,
    stream: &TcpStream,
) -> (Response, Option<Admission>) {
    if request.method == "OPTIONS" {
        return (options_response(state, &request.headers), None);
    }

    let path = request.path();
    let share_mode = state.gate.is_some();
    let mut admission = None;
    if share_mode && path.starts_with("/v1/") {
        let allow_api_key = request.method == "GET" || path.starts_with("/v1/messages");
        match admit_share(state, request, stream, allow_api_key) {
            Ok(value) => admission = Some(value),
            Err(error) => return (error.response(), None),
        }
    }

    let response = match request.method.as_str() {
        "GET" => handle_get(state, request),
        "POST" => handle_post(state, request),
        _ => GatewayError::unknown_path(path).response(),
    };
    (response, admission)
}

fn admit_share(
    state: &AppState,
    request: &Request,
    stream: &TcpStream,
    allow_api_key: bool,
) -> Result<Admission, GatewayError> {
    let path = request.path();
    if path.starts_with("/v1/models") {
        model_catalog_limit(request.raw_target())
            .map_err(|_| GatewayError::route_not_shared())?;
    } else if path.starts_with("/v1/messages") {
        messages_query(request.raw_target())
            .map_err(|_| GatewayError::route_not_shared())?;
    } else if !matches!(path, "/v1/chat/completions" | "/v1/responses") {
        return Err(GatewayError::route_not_shared());
    }
    let gate = state.gate.as_ref().expect("share mode gate");
    let (entry, config) = gate
        .authenticate(&request.headers, allow_api_key)
        .ok_or_else(GatewayError::invalid_api_key)?;
    let mut reason = RefusalReason::default();
    if !gate.acquire(
        &entry.key_id,
        Some(config.config_generation),
        Some(entry.secret_version),
        Some(&mut reason),
    ) {
        return Err(GatewayError::sharing_limit_reached(
            reason.reason.as_deref().unwrap_or(""),
            reason.detail.as_deref(),
        ));
    }
    let context = match stream.try_clone() {
        Ok(clone) => InflightCtx::new(clone),
        Err(_) => {
            gate.release(&entry.key_id);
            return Err(GatewayError::new(
                500,
                "cannot observe the client connection",
                "chat2api_error",
            ));
        }
    };
    gate.register_inflight(&entry.key_id, Arc::clone(&context));
    Ok(Admission {
        key_id: entry.key_id,
        ctx: context,
    })
}

fn handle_get(state: &AppState, request: &Request) -> Response {
    let path = request.path();
    match path {
        "/" | "/health" => Response::json(200, &state.proxy.health_snapshot()),
        "/ready" => {
            let (ready, snapshot) = state.proxy.readiness_snapshot();
            Response::json(if ready { 200 } else { 503 }, &snapshot)
        }
        "/v1/version" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            match state.proxy.version() {
                Ok(payload) => Response::json(200, &payload),
                Err(error) => error.response(),
            }
        }
        "/v1/models" | "/api/models" | "/api/v1/models" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            let raw_fresh = request
                .headers
                .get("X-Chat2Api-Models")
                .is_some_and(|value| value.to_ascii_lowercase().contains("raw-fresh"));
            match state.proxy.models(raw_fresh) {
                Ok((payload, marker)) => {
                    let mut response = Response::json(200, &payload);
                    if marker {
                        response = response.with_header("X-Chat2Api-Raw-Models", "1");
                    }
                    response
                }
                Err(error) => error.response(),
            }
        }
        path if path.starts_with("/admin/") => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            GatewayError::not_implemented("admin_read_surface_s1a").response()
        }
        path if path.starts_with("/api/") => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            GatewayError::not_implemented("local_api_surface_s1a").response()
        }
        _ => GatewayError::unknown_path(path).response(),
    }
}

fn handle_post(state: &AppState, request: &Request) -> Response {
    let path = request.path();
    match path {
        "/admin/share/revoke" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            let body = match read_json_body(
                request,
                BodyPolicy::Console {
                    max_request_bytes: state.config.local_max_request_bytes,
                },
            ) {
                Ok(body) => body,
                Err(error) => return error.response(),
            };
            let body = body.unwrap_or_default();
            if let Err(error) = reject_unknown_fields(&body, &["key_id"]) {
                return error.response();
            }
            let key_id = body.get("key_id").and_then(Value::as_str);
            let Some(key_id) = key_id.filter(|value| !value.is_empty()) else {
                return GatewayError::invalid_request("key_id is required").response();
            };
            let released = state
                .gate
                .as_ref()
                .map(|gate| gate.terminate_key(key_id))
                .unwrap_or(0);
            Response::json(200, &json!({"released": released}))
        }
        "/v1/chat/completions" | "/api/chat/completions"
        | "/api/v1/chat/completions" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            if let Err(error) = origin_allowed(state, request) {
                return error.response();
            }
            let body = match read_json_body(
                request,
                BodyPolicy::Share {
                    max_request_bytes: state.config.share_max_request_bytes,
                },
            ) {
                Ok(body) => body,
                Err(error) => return error.response(),
            };
            let Some(body) = body else {
                return GatewayError::unsupported_chat_request().response();
            };
            if !valid_chat_request(&body) {
                return GatewayError::unsupported_chat_request().response();
            }
            GatewayError::not_implemented("chat_forwarding_s1b").response()
        }
        "/v1/messages" | "/api/v1/messages" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            if let Err(error) = origin_allowed(state, request) {
                return error.response();
            }
            let body = match read_json_body(
                request,
                BodyPolicy::Share {
                    max_request_bytes: state.config.share_max_request_bytes,
                },
            ) {
                Ok(body) => body,
                Err(error) => return error.response(),
            };
            let Some(body) = body else {
                return GatewayError::unsupported_messages_request().response();
            };
            if !valid_messages_request(&body) {
                return GatewayError::unsupported_messages_request().response();
            }
            GatewayError::not_implemented("messages_forwarding_s1b").response()
        }
        "/v1/responses" | "/api/v1/responses" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            if let Err(error) = origin_allowed(state, request) {
                return error.response();
            }
            let body = match read_json_body(
                request,
                BodyPolicy::Share {
                    max_request_bytes: state.config.share_max_request_bytes,
                },
            ) {
                Ok(body) => body,
                Err(error) => return error.response(),
            };
            let Some(body) = body else {
                return GatewayError::unsupported_responses_request().response();
            };
            let allowed = [
                "model",
                "input",
                "instructions",
                "stream",
                "max_output_tokens",
                "temperature",
                "top_p",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "store",
                "prompt_cache_key",
                "prompt_cache_retention",
                "prompt_cache_options",
                "service_tier",
                "reasoning",
                "include",
            ];
            if reject_unknown_fields(&body, &allowed).is_err() {
                return GatewayError::unsupported_responses_request().response();
            }
            GatewayError::not_implemented("responses_forwarding_s2").response()
        }
        "/v1/embeddings" | "/api/v1/embeddings" => {
            if let Err(error) = local_key_allowed(state, request) {
                return error.response();
            }
            if let Err(error) = origin_allowed(state, request) {
                return error.response();
            }
            GatewayError::not_implemented("embeddings_forwarding_s2").response()
        }
        path if path.starts_with("/api/") => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            GatewayError::not_implemented("local_api_surface_s1a").response()
        }
        _ => GatewayError::unknown_path(path).response(),
    }
}

fn options_response(state: &AppState, headers: &Headers) -> Response {
    let mut response = Response::new(204, Vec::<u8>::new());
    if let Some(origin) = cors_allow_origin(state, headers) {
        response = response.with_header("Access-Control-Allow-Origin", origin);
    }
    response
        .with_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        .with_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Api-Key, Anthropic-Version, Anthropic-Beta",
        )
        .with_header("Content-Length", "0")
}

fn origin_allowed(state: &AppState, request: &Request) -> Result<(), GatewayError> {
    let Some(origin) = request.headers.get("Origin") else {
        return Ok(());
    };
    if state.config.allowed_origins.contains(origin) {
        Ok(())
    } else {
        Err(GatewayError::origin_denied())
    }
}

fn cors_allow_origin(state: &AppState, headers: &Headers) -> Option<String> {
    headers
        .get("Origin")
        .filter(|origin| state.config.allowed_origins.contains(*origin))
        .map(str::to_string)
}

fn local_key_allowed(state: &AppState, request: &Request) -> Result<(), GatewayError> {
    if !state.require_local_api_key {
        return Ok(());
    }
    let authorization = request.headers.get("Authorization").unwrap_or_default();
    let mut supplied = if authorization
        .to_ascii_lowercase()
        .starts_with("bearer ")
    {
        authorization[7..].trim()
    } else {
        ""
    };
    if supplied.is_empty() {
        supplied = request.headers.get("X-Api-Key").unwrap_or_default().trim();
    }
    if state.local_api_key.is_empty()
        || !constant_time_eq(supplied.as_bytes(), state.local_api_key.as_bytes())
    {
        return Err(GatewayError::local_auth_error());
    }
    Ok(())
}

fn control_allowed(state: &AppState, request: &Request) -> Result<(), GatewayError> {
    if !is_local_control_request(
        request.peer_ip,
        request.headers.get("Host").unwrap_or_default(),
        request.headers.get("Origin"),
    ) {
        return Err(GatewayError::control_denied_endpoint());
    }
    let expected = state.control_token.as_deref().unwrap_or_default();
    let supplied = request
        .headers
        .get("X-Admin-Control")
        .unwrap_or_default();
    if expected.is_empty()
        || !constant_time_eq(supplied.as_bytes(), expected.as_bytes())
    {
        return Err(GatewayError::control_denied_token());
    }
    Ok(())
}

/// Python `is_local_control_request`: loopback peer, no Origin, loopback Host.
pub fn is_local_control_request(peer_ip: IpAddr, host: &str, origin: Option<&str>) -> bool {
    if !peer_ip.is_loopback() {
        return false;
    }
    if origin.is_some() {
        return false;
    }
    let host = host.trim();
    let hostname = if let Some(rest) = host.strip_prefix('[') {
        rest.split(']').next().unwrap_or_default()
    } else {
        host.split(':').next().unwrap_or_default()
    };
    matches!(hostname.to_ascii_lowercase().as_str(), "127.0.0.1" | "localhost" | "::1")
}

/// Accept only Harness's bounded catalog query.
pub fn model_catalog_limit(raw_target: &str) -> Result<Option<u32>, ()> {
    if raw_target == "/v1/models" {
        return Ok(None);
    }
    let Some(query) = raw_target.strip_prefix("/v1/models?") else {
        return Err(());
    };
    if query.is_empty() {
        return Err(());
    }
    let mut limit = None;
    let mut seen = std::collections::BTreeSet::new();
    for part in query.split('&') {
        let (name, value) = part.split_once('=').ok_or(())?;
        if name.is_empty() || !seen.insert(name) {
            return Err(());
        }
        if name == "beta" && value == "true" {
            continue;
        }
        if name != "limit" {
            return Err(());
        }
        if !value.is_ascii() || !value.chars().all(|character| character.is_ascii_digit()) {
            return Err(());
        }
        if !(1..=4).contains(&value.len()) {
            return Err(());
        }
        let parsed = value.parse::<u32>().map_err(|_| ())?;
        if !(1..=1000).contains(&parsed) || parsed.to_string() != value {
            return Err(());
        }
        limit = Some(parsed);
    }
    Ok(limit)
}

/// Accept the SDK's beta query on Messages, never an arbitrary one.
pub fn messages_query(raw_target: &str) -> Result<(), ()> {
    if raw_target == "/v1/messages" || raw_target == "/v1/messages?beta=true" {
        Ok(())
    } else {
        Err(())
    }
}

/// Shape-only admission for one shared Chat Completions body.
pub fn valid_chat_request(body: &serde_json::Map<String, Value>) -> bool {
    let allowed = [
        "model",
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "reasoning_effort",
        "store",
    ];
    if body.keys().any(|key| !allowed.contains(&key.as_str())) {
        return false;
    }
    if !body.get("model").and_then(Value::as_str).is_some_and(|value| !value.is_empty()) {
        return false;
    }
    if !body.get("messages").and_then(Value::as_array).is_some_and(|value| !value.is_empty()) {
        return false;
    }
    if !matches!(body.get("stream"), None | Some(Value::Bool(_))) {
        return false;
    }
    !body.contains_key("store") || body.get("store") == Some(&Value::Bool(false))
}

/// Shape-only admission for one shared Messages body.
pub fn valid_messages_request(body: &serde_json::Map<String, Value>) -> bool {
    let allowed = [
        "max_tokens",
        "messages",
        "metadata",
        "model",
        "stop_sequences",
        "stream",
        "system",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    ];
    if body.keys().any(|key| !allowed.contains(&key.as_str())) {
        return false;
    }
    if !body.get("model").and_then(Value::as_str).is_some_and(|value| !value.is_empty()) {
        return false;
    }
    if !body.get("messages").and_then(Value::as_array).is_some_and(|value| !value.is_empty()) {
        return false;
    }
    if !body
        .get("max_tokens")
        .and_then(Value::as_u64)
        .is_some_and(|value| value >= 1)
    {
        return false;
    }
    body.get("stream").map_or(true, Value::is_boolean)
}

/// Test seam: the body policy used for a route group.
pub fn body_policy_for(state: &AppState, share: bool) -> BodyPolicy {
    if share {
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        }
    } else {
        BodyPolicy::Console {
            max_request_bytes: state.config.local_max_request_bytes,
        }
    }
}

/// Test seam: the parsed body state of one request.
pub fn body_bytes(request: &Request) -> Option<&[u8]> {
    match &request.body {
        BodyRead::Complete(bytes) => Some(bytes),
        _ => None,
    }
}

/// Test seam: the share-config auth mode.
pub fn auth_mode(config: &GatewayConfig) -> AuthMode {
    config.auth_mode
}
