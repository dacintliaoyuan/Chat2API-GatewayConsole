//! Console 8002 HTTP server.
//!
//! Candidate mode (the default) binds only an ephemeral loopback port;
//! production mode binds only `127.0.0.1:8002`. It serves the S5a
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

use um_gateway::http::{
    read_request_with_leftover, write_response, BodyRead, Headers, Request, Response,
};

use crate::actions::action_catalogue;
use crate::body::{
    read_console_body, reject_duplicate_sensitive_header, reject_unknown_fields,
    CONSOLE_MAX_REQUEST_BYTES,
};
use crate::cloud_ops::cloud_ops_status;
use crate::config::{ConfigError, ConsoleConfig, Role};
use crate::display::{display_session, display_state};
use crate::display_grant::{DisplayGrants, DISPLAY_SESSION_COOKIE};
use crate::display_relay::{DisplayRelay, WEBSOCKET_PATH};
use crate::error::ConsoleError;
use crate::gateway_connection;
use crate::local_um::{self, UmQueryError};
use crate::metering::{self, UsageFilters};
use crate::operation_manager::{OperationError, OperationManagerSlot, UnwiredActionExecutor};
use crate::operations::OperationsReader;
use crate::runtime::{LinuxPidFileFacts, LocalTcpFacts, RuntimeFacts, RuntimeSnapshot};
use crate::session::{SessionState, SESSION_COOKIE, SESSION_TTL_SECONDS};
use crate::share_auth::{control_token_ok, management_write_auth, ManagementAuth};
use crate::share_read::{control_status, DisplaySeams, ShareReadSeams, ShareReadStore};
use crate::share_write::{
    console_public_row, ShareWriteError, ShareWriteSeams, ShareWriteStore, UnwiredWriteSeams,
};
use crate::local_logs::{self, LogPaths};
use crate::local_stats::{self, StatsPaths};
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
    pub operation_manager: OperationManagerSlot,
    pub share: ShareReadStore,
    pub share_seams: Arc<dyn ShareReadSeams>,
    pub share_write: ShareWriteStore,
    pub session: SessionState,
    pub display_grants: DisplayGrants,
    pub display: DisplayRelay,
    local_ledger: std::path::PathBuf,
    local_service_log: std::path::PathBuf,
    local_watch_dir: Option<std::path::PathBuf>,
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
        config.validate_bind_policy().map_err(ServerError::Config)?;
        let runtime: Arc<dyn RuntimeFacts> = match config.role {
            Role::Local => Arc::new(LocalTcpFacts::new(config.gateway_port)),
            Role::Cloud => Arc::new(LinuxPidFileFacts::from_config(&config)),
        };
        Self::start_with_runtime(config, runtime)
    }

    pub fn start_with_runtime(
        config: ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
    ) -> Result<ServerHandle, ServerError> {
        let display = DisplaySeams::new(config.display.availability());
        Self::start_with_seams(config, runtime, Arc::new(display))
    }

    pub fn start_with_seams(
        config: ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
        share_seams: Arc<dyn ShareReadSeams>,
    ) -> Result<ServerHandle, ServerError> {
        Self::start_with_wiring(
            config,
            runtime,
            share_seams,
            Arc::new(UnwiredActionExecutor),
            Arc::new(UnwiredWriteSeams),
        )
    }

    pub fn start_with_wiring(
        config: ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
        share_seams: Arc<dyn ShareReadSeams>,
        executor: Arc<dyn crate::operation_manager::ActionExecutor>,
        write_seams: Arc<dyn ShareWriteSeams>,
    ) -> Result<ServerHandle, ServerError> {
        config.validate_bind_policy().map_err(ServerError::Config)?;
        let operation_manager = OperationManagerSlot::new(
            config.console_state_file.clone(),
            config.console_lock_dir.clone(),
            executor,
        );
        let state = Arc::new(AppState {
            operations: OperationsReader::new(config.console_state_file.clone()),
            operation_manager,
            share: ShareReadStore::with_options(
                config.share_store_file.clone().unwrap_or_default(),
                config.public_origin.clone(),
                config.share_secret_key_file.clone(),
            ),
            share_seams,
            share_write: ShareWriteStore::from_config(
                config.share_store_file.clone(),
                config.share_secret_key_file.clone(),
                write_seams,
            ),
            session: SessionState::with_code_file(config.usage_session_code_file.clone()),
            display_grants: DisplayGrants::new(),
            display: DisplayRelay::new(config.display.clone()),
            local_ledger: config.usage_log.clone(),
            local_service_log: config.service_log.clone(),
            local_watch_dir: config.watch_dir.clone(),
            config: config.clone(),
            runtime,
            summary: SummaryCache::for_config(&config),
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
    Console(String),
    Io(std::io::Error),
}

impl std::fmt::Display for ServerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Config(error) => write!(formatter, "{error}"),
            Self::Console(error) => write!(formatter, "{error}"),
            Self::Io(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ServerError {}

fn handle_connection(state: Arc<AppState>, mut stream: TcpStream) {
    // The accept loop polls in non-blocking mode; Windows copies that mode into
    // accepted sockets, so restore blocking reads and let the read timeout cover
    // a client that connects before it sends its request.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    loop {
        let (request, leftover) = match read_request_with_leftover(
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
        let console_port = stream
            .local_addr()
            .map(|addr| addr.port())
            .unwrap_or(state.config.port);
        if request.method == "GET" && request.path() == WEBSOCKET_PATH {
            state.display.handle_websocket(
                &request,
                leftover,
                &mut stream,
                console_port,
                &state.display_grants,
                &state.session,
                state.config.control_token_file.as_deref(),
            );
            return;
        }
        let response = dispatch(&state, &request, console_port);
        let close = response.close;
        if write_response(&mut stream, &response).is_err() || close {
            return;
        }
    }
}

fn dispatch(state: &AppState, request: &Request, console_port: u16) -> Response {
    if !host_allowed(request.headers.get("Host").unwrap_or_default())
        || !origin_allowed(request.headers.get("Origin"), console_port)
        || (state.config.role == Role::Local && has_proxy_metadata(&request.headers))
    {
        return ConsoleError::local_only().response();
    }
    let target = rewrite_alias(request.raw_target());
    let path = target.split('?').next().unwrap_or(&target);
    let method = request.method.as_str();

    if path == "/display" || path.starts_with("/display/") {
        let response = match method {
            "GET" => state.display.handle_get(
                request,
                console_port,
                &state.display_grants,
                &state.session,
                state.config.control_token_file.as_deref(),
            ),
            "POST" => state.display.handle_post(
                request,
                console_port,
                &state.display_grants,
                &state.session,
                state.config.control_token_file.as_deref(),
            ),
            _ => None,
        };
        return response.unwrap_or_else(|| ConsoleError::unknown_path().response());
    }

    if path == "/" || path == "/index.html" {
        if method != "GET" || state.config.role != Role::Local {
            return ConsoleError::unknown_path().response();
        }
        return crate::static_files::serve_index(
            &state.config.console_dist_dir,
            state.config.role,
            state.config.port,
        );
    }

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
    if path == "/api/session" {
        if method == "POST" {
            return handle_session_exchange(state, request);
        }
        return ConsoleError::unknown_path().response();
    }
    if path == "/api/session/reissue" {
        if method == "POST" {
            return handle_session_reissue(state, request);
        }
        return ConsoleError::unknown_path().response();
    }
    if path.starts_with("/api/share/") {
        return handle_share_api(state, request, &target);
    }
    if !path.starts_with("/api/console/") {
        if method == "GET" && state.config.role == Role::Local {
            return crate::static_files::serve_static(&state.config.console_dist_dir, path);
        }
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
    if state.config.role == Role::Local {
        if let Some(response) = handle_local_usage_read(state, method, &rest, &target) {
            return response;
        }
        if rest.first() == Some(&"um") {
            if method != "GET" {
                return ConsoleError::unknown_path().response();
            }
            return handle_local_um(state, &rest, &target);
        }
    }
    match method {
        "GET" => handle_console_get(state, &rest, &target),
        "POST" | "PATCH" | "DELETE" if rest.first() == Some(&"share-management") => {
            handle_share_management_write(state, method, &rest[1..], request)
        }
        "POST" | "PATCH" | "DELETE" => handle_console_write(state, method, &rest, request),
        _ => ConsoleError::unknown_path().response(),
    }
}

/// `usagesrv.py::do_POST /api/session`: a lenient body read (any JSON
/// failure is the empty object) then a one-time bootstrap-code exchange.
fn handle_session_exchange(state: &AppState, request: &Request) -> Response {
    if let Err(error) = reject_duplicate_sensitive_header(&request.headers) {
        return error.response();
    }
    let body = session_body(request);
    let code = body.get("code").and_then(Value::as_str);
    let Some(token) = state.session.exchange(code) else {
        return ConsoleError::new(403, json!({"error": "invalid bootstrap code"})).response();
    };
    let cookie = format!(
        "{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}"
    );
    send_json(200, &json!({"ok": true})).with_header("Set-Cookie", cookie)
}

/// `usagesrv.py::do_POST /api/session/reissue`: the local control token is
/// required; the new code invalidates the previous one.
fn handle_session_reissue(state: &AppState, request: &Request) -> Response {
    if let Err(error) = reject_duplicate_sensitive_header(&request.headers) {
        return error.response();
    }
    let token_ok = control_token_ok(
        state.config.control_token_file.as_deref(),
        request.headers.get("X-Admin-Control"),
    );
    if !token_ok {
        return ConsoleError::new(403, json!({"error": "control token required"})).response();
    }
    match state.session.reissue(true) {
        Some(code) => send_json(200, &json!({"ok": true, "code": code})),
        None => ConsoleError::new(403, json!({"error": "control token required"})).response(),
    }
}

/// `usagesrv.py::do_POST /api/session` body: `json.loads` failure is the
/// empty object; the route never rejects the body shape.
fn session_body(request: &Request) -> Map<String, Value> {
    match &request.body {
        BodyRead::Complete(bytes) if !bytes.is_empty() => {
            match serde_json::from_slice::<Value>(bytes) {
                Ok(Value::Object(map)) => map,
                _ => Map::new(),
            }
        }
        _ => Map::new(),
    }
}

fn handle_share_api(state: &AppState, request: &Request, raw_target: &str) -> Response {
    if let Err(error) = reject_duplicate_sensitive_header(&request.headers) {
        return error.response();
    }
    let method = request.method.as_str();
    let write = method != "GET";
    let auth = management_write_auth(
        &state.session,
        state.config.control_token_file.as_deref(),
        request.headers.get("Cookie"),
        request.headers.get("X-Admin-Control"),
        request.headers.get("X-Requested-With"),
        write,
    );
    match auth {
        ManagementAuth::Control | ManagementAuth::Session => {}
        ManagementAuth::Missing => {
            let mut response = ConsoleError::new(
                401,
                json!({"error": "share management credentials required"}),
            )
            .response();
            response.close = true;
            return response;
        }
        ManagementAuth::CsrfRequired => {
            let mut response =
                ConsoleError::new(403, json!({"error": "session CSRF marker required"})).response();
            response.close = true;
            return response;
        }
    }
    let path = raw_target.split('?').next().unwrap_or(raw_target);
    let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    let rest: Vec<&str> = segments.iter().skip(2).copied().collect();
    if !write {
        return match rest.as_slice() {
            ["usage"] => share_usage_response(state, raw_target, false),
            ["usage", "requests"] => share_usage_response(state, raw_target, true),
            ["status"] => match state.share.raw_status(state.share_seams.broker_snapshot()) {
                Ok(payload) => send_json(200, &with_last_publication_failure(state, payload)),
                Err(error) => error.response(),
            },
            ["keys"] => match state.share.share_keys() {
                Ok(payload) => send_json(200, &payload),
                Err(error) => error.response(),
            },
            ["keys", key_id] => match state.share.share_key(key_id) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => error.response(),
            },
            ["keys", key_id, "secret"] => {
                let expected = query_value(raw_target, "expected_row_version");
                match state.share.secret(key_id, expected.as_deref()) {
                    Ok((status, payload)) => send_json(status, &payload),
                    Err(error) => error.response(),
                }
            }
            ["operations", operation_id] => match state.share.operation(operation_id) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => error.response(),
            },
            _ => ConsoleError::unknown_path().response(),
        };
    }
    let body = match read_console_body(request) {
        Ok(body) => body,
        Err(error) => return error.response(),
    };
    let writer = &state.share_write;
    match (method, rest.as_slice()) {
        ("POST", ["limits", "normalize"]) => {
            if let Err(error) = reject_unknown_fields(&body, &[]) {
                return error.response();
            }
            match writer.normalize_limits() {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["keys"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["name", "idempotency_key", "max_concurrency"])
            {
                return error.response();
            }
            let name = match body.get("name") {
                Some(Value::String(name)) if (1..=64).contains(&name.chars().count()) => {
                    name.clone()
                }
                _ => return ConsoleError::body_open(400, "invalid name").response(),
            };
            let idempotency_key = match optional_idempotency_key(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            let max_concurrency = match optional_max_concurrency(
                &body,
                SHARE_KEY_MAX_CONCURRENCY_MAX,
                "max_concurrency must be an integer in 1..48",
            ) {
                Ok(value) => value,
                Err(response) => return response,
            };
            match writer.create_key_legacy(&name, max_concurrency, idempotency_key.as_deref()) {
                Ok(payload) => send_json(201, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["keys", key_id, "test"]) => {
            if let Err(error) = reject_unknown_fields(&body, &["expected_row_version"]) {
                return error.response();
            }
            let expected = match required_positive_expected_version(
                &body,
                "invalid key_id or expected_row_version",
            ) {
                Ok(value) => value,
                Err(response) => return response,
            };
            if key_id.is_empty() || key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id or expected_row_version")
                    .response();
            }
            let payload = writer.test_key(
                key_id,
                expected,
                false,
                state.config.public_origin.as_deref(),
            );
            send_json(200, &payload)
        }
        ("POST", ["keys", key_id, "rotate"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["idempotency_key", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let idempotency_key = match optional_idempotency_key(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            let expected = match required_expected_version(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            match writer.rotate_key_legacy(key_id, idempotency_key.as_deref(), expected) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["keys", key_id, "revoke-and-terminate"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["idempotency_key", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let idempotency_key = match optional_idempotency_key(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            let expected = match required_positive_expected_version(
                &body,
                "expected_row_version must be a positive integer",
            ) {
                Ok(value) => value,
                Err(response) => return response,
            };
            match writer.revoke_and_terminate_key(key_id, expected, idempotency_key.as_deref()) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("PATCH", ["keys", key_id]) => {
            if body.contains_key("enabled") {
                return ConsoleError::body_open(400, "enabled_not_writable").response();
            }
            if let Err(error) =
                reject_unknown_fields(&body, &["name", "max_concurrency", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let name = match body.get("name") {
                None | Some(Value::Null) => None,
                Some(Value::String(name)) if (1..=64).contains(&name.chars().count()) => {
                    Some(name.clone())
                }
                _ => return ConsoleError::body_open(400, "invalid name").response(),
            };
            let max_concurrency = match optional_max_concurrency(
                &body,
                SHARE_KEY_MAX_CONCURRENCY_MAX,
                "max_concurrency must be an integer in 1..48",
            ) {
                Ok(value) => value,
                Err(response) => return response,
            };
            let expected = match required_expected_version(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            match writer.update_key(key_id, name.as_deref(), max_concurrency, expected) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("PATCH", ["settings"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["share_max_concurrency", "expected_share_max"])
            {
                return error.response();
            }
            let share_max = match body.get("share_max_concurrency") {
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) if (1..=64).contains(&value) => value,
                    _ => {
                        return ConsoleError::body_open(
                            400,
                            "share_max_concurrency must be an integer in 1..64",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(
                        400,
                        "share_max_concurrency must be an integer in 1..64",
                    )
                    .response();
                }
            };
            let expected = match body.get("expected_share_max") {
                None | Some(Value::Null) => None,
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) => Some(value),
                    None => {
                        return ConsoleError::body_open(
                            400,
                            "expected_share_max must be an integer",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(400, "expected_share_max must be an integer")
                        .response();
                }
            };
            match writer.set_share_limits(share_max, expected) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("DELETE", ["keys", key_id]) => {
            if let Err(error) = reject_unknown_fields(&body, &["expected_row_version"]) {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let expected = match required_expected_version(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            match writer.delete_key(key_id, expected) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["start"]) | ("POST", ["stop"]) | ("POST", ["emergency-stop"]) => {
            if let Err(error) = reject_unknown_fields(&body, &["idempotency_key"]) {
                return error.response();
            }
            let idempotency_key = match optional_idempotency_key(&body) {
                Ok(value) => value,
                Err(response) => return response,
            };
            let result = match rest.as_slice() {
                ["start"] => writer.start(false, idempotency_key.as_deref()),
                ["stop"] => writer.stop(idempotency_key.as_deref()),
                _ => writer.emergency_stop_legacy(idempotency_key.as_deref()),
            };
            match result {
                Ok(payload) => send_json(202, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        _ => ConsoleError::unknown_path().response(),
    }
}

const SHARE_KEY_MAX_CONCURRENCY_MAX: i64 = 48;

fn optional_idempotency_key(body: &Map<String, Value>) -> Result<Option<String>, Response> {
    match body.get("idempotency_key") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(key)) if (1..=128).contains(&key.chars().count()) => {
            Ok(Some(key.clone()))
        }
        _ => Err(ConsoleError::body_open(400, "invalid idempotency_key").response()),
    }
}

fn optional_max_concurrency(
    body: &Map<String, Value>,
    max: i64,
    message: &str,
) -> Result<Option<i64>, Response> {
    match body.get("max_concurrency") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(number)) => match number.as_i64() {
            Some(value) if (1..=max).contains(&value) => Ok(Some(value)),
            _ => Err(ConsoleError::body_open(400, message).response()),
        },
        _ => Err(ConsoleError::body_open(400, message).response()),
    }
}

fn required_expected_version(body: &Map<String, Value>) -> Result<i64, Response> {
    required_positive_expected_version(body, "expected_row_version required")
}

fn required_positive_expected_version(
    body: &Map<String, Value>,
    message: &str,
) -> Result<i64, Response> {
    match body.get("expected_row_version") {
        Some(Value::Number(number)) => match number.as_i64() {
            Some(value) if value >= 1 => Ok(value),
            _ => Err(ConsoleError::body_open(400, message).response()),
        },
        _ => Err(ConsoleError::body_open(400, message).response()),
    }
}

/// `usagesrv.py` local usage faces: usage-tokens/usage-rates/stats read the
/// private chat2api ledger and the frozen DSH snapshot. Only exact GET paths
/// are assembled; anything else keeps falling through to the unknown-path answer.
fn handle_local_usage_read(
    state: &AppState,
    method: &str,
    rest: &[&str],
    raw_target: &str,
) -> Option<Response> {
    if method != "GET" {
        return None;
    }
    let paths = || StatsPaths {
        usage_log: state.config.usage_log.clone(),
        dsh_snapshot: state.config.dsh_snapshot.clone(),
    };
    match rest {
        ["usage-tokens"] => {
            let days = match query_value(raw_target, "days") {
                None => 14,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(days) => days,
                    Err(_) => {
                        return Some(
                            ConsoleError::new(
                                400,
                                json!({"error": "invalid_usage_query",
                                        "detail": "days 必须是整数"}),
                            )
                            .response(),
                        );
                    }
                },
            };
            Some(send_json(
                200,
                &local_stats::usage_tokens(&paths(), days, epoch_seconds() as f64 * 1000.0),
            ))
        }
        ["log-sources"] => Some(send_json(
            200,
            &local_logs::registry_payload(&LogPaths::from_config(&state.config)),
        )),
        ["stats"] => {
            let date_from = query_value(raw_target, "from").unwrap_or_default();
            let date_to = query_value(raw_target, "to").unwrap_or_default();
            let csv_list = |name: &str| {
                let raw = query_value(raw_target, name).unwrap_or_default();
                let values: Vec<String> = raw
                    .split(',')
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
                    .collect();
                if values.is_empty() {
                    None
                } else {
                    Some(values)
                }
            };
            let sources = csv_list("sources");
            let models = csv_list("models");
            match local_stats::bounded_stats(
                &paths(),
                &date_from,
                &date_to,
                sources.as_deref(),
                models.as_deref(),
            ) {
                Ok(payload) => Some(send_json(200, &payload)),
                Err(error) => Some(
                    ConsoleError::new(
                        400,
                        json!({"error": "invalid_stats_query", "detail": error.0}),
                    )
                    .response(),
                ),
            }
        }
        ["usage-rates"] => {
            let hours = match query_value(raw_target, "hours") {
                None => 24,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(hours) => hours,
                    Err(_) => {
                        return Some(
                            ConsoleError::new(
                                400,
                                json!({"error": "invalid_usage_query",
                                        "detail": "hours 必须是整数"}),
                            )
                            .response(),
                        );
                    }
                },
            };
            Some(send_json(
                200,
                &local_stats::usage_rates(&paths(), hours, epoch_seconds() as f64 * 1000.0),
            ))
        }
        _ => None,
    }
}

fn handle_local_um(state: &AppState, rest: &[&str], raw_target: &str) -> Response {
    if rest.len() != 2 {
        return ConsoleError::unknown_path().response();
    }
    let query = query_values(raw_target);
    let value = |name: &str| query.get(name).and_then(|values| values.first()).cloned();
    let now_ms = epoch_seconds() as f64 * 1000.0;
    let result = match rest[1] {
        "today" => Ok(local_um::um_today(&state.local_ledger, now_ms)),
        "trend" => local_um::um_trend(
            &state.local_ledger,
            value("range").as_deref().unwrap_or("7d"),
            now_ms,
        ),
        "requests" => {
            let limit = match value("limit") {
                None => 50,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(limit) => limit,
                    Err(_) => {
                        return ConsoleError::new(
                            400,
                            json!({"error": "invalid_um_query", "detail": "limit 必须是整数"}),
                        )
                        .response();
                    }
                },
            };
            local_um::um_requests(
                &state.local_ledger,
                value("window").as_deref().unwrap_or("today"),
                value("model").as_deref(),
                value("result").as_deref().unwrap_or("all"),
                value("search").as_deref().unwrap_or(""),
                value("cursor").as_deref(),
                limit,
                now_ms,
            )
        }
        "logs" => {
            let limit = match value("limit") {
                None => 80,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(limit) => limit,
                    Err(_) => {
                        return ConsoleError::new(
                            400,
                            json!({"error": "invalid_um_query", "detail": "limit 必须是整数"}),
                        )
                        .response();
                    }
                },
            };
            let levels: Vec<String> = value("levels")
                .unwrap_or_default()
                .split(',')
                .map(|level| level.trim().to_string())
                .filter(|level| !level.is_empty())
                .collect();
            let payload = local_um::um_logs(
                &state.local_service_log,
                state.local_watch_dir.as_deref(),
                value("window").as_deref().unwrap_or("today"),
                &levels,
                value("source").as_deref().unwrap_or("service"),
                value("cursor").as_deref(),
                limit,
                now_ms,
            );
            if payload["status"] == json!("error") {
                return ConsoleError::new(
                    400,
                    json!({"error": "invalid_um_query", "detail": payload["error"]}),
                )
                .response();
            }
            return send_json(200, &payload);
        }
        "request-logs" => {
            let limit = match value("limit") {
                None => 100,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(limit) => limit,
                    Err(_) => {
                        return ConsoleError::new(
                            400,
                            json!({"error": "invalid_um_query", "detail": "limit 必须是整数"}),
                        )
                        .response();
                    }
                },
            };
            local_um::um_request_logs(
                &state.local_service_log,
                state.local_watch_dir.as_deref(),
                value("request_id").as_deref().unwrap_or(""),
                limit,
                now_ms,
            )
        }
        "maintenance" => {
            let limit = match value("limit") {
                None => 50,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(limit) => limit,
                    Err(_) => {
                        return ConsoleError::new(
                            400,
                            json!({"error": "invalid_um_query", "detail": "limit 必须是整数"}),
                        )
                        .response();
                    }
                },
            };
            Ok(local_um::um_maintenance(&state.operations, limit))
        }
        _ => return ConsoleError::unknown_path().response(),
    };
    match result {
        Ok(payload) => send_json(200, &payload),
        Err(error) => invalid_um_query(&error),
    }
}

fn invalid_um_query(error: &UmQueryError) -> Response {
    ConsoleError::new(
        400,
        json!({"error": "invalid_um_query", "detail": error.0}),
    )
    .response()
}

fn handle_console_get(state: &AppState, rest: &[&str], raw_target: &str) -> Response {
    match rest {
        ["services"] => services_response(state),
        ["health"] => health_response(state),
        ["actions"] => send_json(
            200,
            &if state.config.role == Role::Local {
                crate::actions::action_catalogue_local()
            } else {
                action_catalogue()
            },
        ),
        ["share-management", "status"] => match state.share.status(&*state.share_seams) {
            Ok(payload) => send_json(200, &with_last_publication_failure(state, payload)),
            Err(error) => error.response(),
        },
        ["share-management", "keys"] => match state.share.keys() {
            Ok(payload) => send_json(200, &payload),
            Err(error) => error.response(),
        },
        ["share-management", "control-status"] => {
            send_json(200, &control_status(state.share_seams.broker_snapshot()))
        }
        ["share-management", "keys", key_id, "secret"] => {
            let expected = query_value(raw_target, "expected_row_version");
            match state.share.secret(key_id, expected.as_deref()) {
                Ok((status, payload)) => send_json(status, &payload),
                Err(error) => error.response(),
            }
        }
        ["share-usage"] => share_usage_response(state, raw_target, false),
        ["share-usage", "requests"] => share_usage_response(state, raw_target, true),
        ["cloud-ops"] => send_json(
            200,
            &cloud_ops_status(state.config.security_state_file.as_deref()),
        ),
        ["gateway-connection"] => gateway_connection_response(state, raw_target),
        ["share-management", "display-session"] => send_json(
            200,
            &display_session(state.share_seams.display_availability()),
        ),
        ["share-management", "display-state"] => send_json(
            200,
            &display_state(
                state.share_seams.auth_observation(),
                state.share_seams.display_availability(),
            ),
        ),
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

fn handle_share_management_write(
    state: &AppState,
    method: &str,
    rest: &[&str],
    request: &Request,
) -> Response {
    let token_ok = control_token_ok(
        state.config.control_token_file.as_deref(),
        request.headers.get("X-Admin-Control"),
    );
    if !token_ok && request.headers.get("X-Requested-With") != Some("XMLHttpRequest") {
        let mut response = ConsoleError::new(
            403,
            json!({"error": "same-origin marker or control token required"}),
        )
        .response();
        response.close = true;
        return response;
    }
    if method == "POST" && rest == ["display-grant"] {
        return handle_display_grant(state, request);
    }
    if method == "POST" && rest == ["display-operator"] {
        return handle_display_operator(state, request);
    }
    if rest == ["control-status"] {
        let mut response =
            ConsoleError::new(405, json!({"error": "method not allowed"})).response();
        response.close = true;
        return response;
    }
    let body = match read_console_body(request) {
        Ok(body) => body,
        Err(error) => return error.response(),
    };
    let writer = &state.share_write;
    match (method, rest) {
        ("POST", ["keys"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["name", "idempotency_key", "max_concurrency"])
            {
                return error.response();
            }
            let name = match body.get("name") {
                Some(Value::String(name)) if (1..=64).contains(&name.chars().count()) => {
                    name.clone()
                }
                _ => return ConsoleError::body_open(400, "invalid name").response(),
            };
            let idempotency_key = match body.get("idempotency_key") {
                None | Some(Value::Null) => None,
                Some(Value::String(key)) if (1..=128).contains(&key.chars().count()) => {
                    Some(key.as_str())
                }
                _ => {
                    return ConsoleError::body_open(400, "invalid idempotency_key").response();
                }
            };
            let max_concurrency = match body.get("max_concurrency") {
                None | Some(Value::Null) => None,
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) if (1..=48).contains(&value) => Some(value),
                    _ => {
                        return ConsoleError::body_open(
                            400,
                            "max_concurrency must be an integer in 1..48",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(
                        400,
                        "max_concurrency must be an integer in 1..48",
                    )
                    .response();
                }
            };
            match writer.create_key(&name, max_concurrency, idempotency_key) {
                Ok(payload) => send_json(
                    201,
                    &json!({
                        "key": payload["key"].clone(),
                        "plaintext": payload["plaintext"],
                    }),
                ),
                Err(error) => share_write_error(&error),
            }
        }
        ("PATCH", ["settings"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["share_max_concurrency", "expected_share_max"])
            {
                return error.response();
            }
            let share_max = match body.get("share_max_concurrency") {
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) if (1..=64).contains(&value) => value,
                    _ => {
                        return ConsoleError::body_open(
                            400,
                            "share_max_concurrency must be an integer in 1..64",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(
                        400,
                        "share_max_concurrency must be an integer in 1..64",
                    )
                    .response();
                }
            };
            let expected = match body.get("expected_share_max") {
                None | Some(Value::Null) => None,
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) => Some(value),
                    None => {
                        return ConsoleError::body_open(
                            400,
                            "expected_share_max must be an integer",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(400, "expected_share_max must be an integer")
                        .response();
                }
            };
            match writer.set_share_limits(share_max, expected) {
                Ok(payload) => send_json(200, &payload),
                Err(error) => share_write_error(&error),
            }
        }
        ("PATCH", ["keys", key_id]) => {
            if body.contains_key("enabled") {
                return ConsoleError::body_open(400, "enabled_not_writable").response();
            }
            if let Err(error) =
                reject_unknown_fields(&body, &["name", "max_concurrency", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let name = match body.get("name") {
                None | Some(Value::Null) => None,
                Some(Value::String(name)) if (1..=64).contains(&name.chars().count()) => {
                    Some(name.as_str())
                }
                _ => return ConsoleError::body_open(400, "invalid name").response(),
            };
            let max_concurrency = match body.get("max_concurrency") {
                None | Some(Value::Null) => None,
                Some(Value::Number(number)) => match number.as_i64() {
                    Some(value) if (1..=48).contains(&value) => Some(value),
                    _ => {
                        return ConsoleError::body_open(
                            400,
                            "max_concurrency must be an integer in 1..48",
                        )
                        .response();
                    }
                },
                _ => {
                    return ConsoleError::body_open(
                        400,
                        "max_concurrency must be an integer in 1..48",
                    )
                    .response();
                }
            };
            let expected = match body.get("expected_row_version") {
                Some(Value::Number(number)) => number.as_i64().filter(|value| *value >= 1),
                _ => None,
            };
            let Some(expected) = expected else {
                return ConsoleError::body_open(400, "expected_row_version required").response();
            };
            match writer.update_key(key_id, name, max_concurrency, expected) {
                Ok(payload) => send_json(200, &json!({"key": console_public_row(&payload)})),
                Err(error) => share_write_error(&error),
            }
        }
        ("DELETE", ["keys", key_id]) => {
            if let Err(error) = reject_unknown_fields(&body, &["expected_row_version"]) {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let expected = match body.get("expected_row_version") {
                Some(Value::Number(number)) => number.as_i64().filter(|value| *value >= 1),
                _ => None,
            };
            let Some(expected) = expected else {
                return ConsoleError::body_open(400, "expected_row_version required").response();
            };
            match writer.delete_key(key_id, expected) {
                Ok(payload) => send_json(
                    200,
                    &json!({
                        "ok": true,
                        "deleted": key_id,
                        "deleted_at": payload.get("deleted_at").cloned().unwrap_or(Value::Null),
                    }),
                ),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["keys", key_id, "rotate"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["idempotency_key", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let idempotency_key = match body.get("idempotency_key") {
                None | Some(Value::Null) => None,
                Some(Value::String(key)) if (1..=128).contains(&key.chars().count()) => {
                    Some(key.as_str())
                }
                _ => {
                    return ConsoleError::body_open(400, "invalid idempotency_key").response();
                }
            };
            let expected = match body.get("expected_row_version") {
                Some(Value::Number(number)) => number.as_i64().filter(|value| *value >= 1),
                _ => None,
            };
            let Some(expected) = expected else {
                return ConsoleError::body_open(400, "expected_row_version required").response();
            };
            match writer.rotate_key(key_id, idempotency_key, expected) {
                Ok(payload) => send_json(
                    200,
                    &json!({
                        "key": payload["key"].clone(),
                        "plaintext": payload["plaintext"],
                    }),
                ),
                Err(error) => share_write_error(&error),
            }
        }
        ("POST", ["keys", key_id, "revoke-and-terminate"]) => {
            if let Err(error) =
                reject_unknown_fields(&body, &["idempotency_key", "expected_row_version"])
            {
                return error.response();
            }
            if key_id.chars().count() > 128 {
                return ConsoleError::body_open(400, "invalid key_id").response();
            }
            let idempotency_key = match body.get("idempotency_key") {
                None | Some(Value::Null) => None,
                Some(Value::String(key)) if (1..=128).contains(&key.chars().count()) => {
                    Some(key.as_str())
                }
                _ => {
                    return ConsoleError::body_open(400, "invalid idempotency_key").response();
                }
            };
            let expected = match body.get("expected_row_version") {
                Some(Value::Number(number)) => number.as_i64().filter(|value| *value >= 1),
                _ => None,
            };
            let Some(expected) = expected else {
                return ConsoleError::body_open(
                    400,
                    "expected_row_version must be a positive integer",
                )
                .response();
            };
            match writer.revoke_and_terminate_key(key_id, expected, idempotency_key) {
                Ok(payload) => send_json(
                    200,
                    &json!({
                        "key": console_public_row(&payload["key"]),
                        "revoked": true,
                        "terminated": payload["terminated"],
                        "released": payload["released"],
                        "aborted": payload["aborted"],
                        "upstream_closed": payload["upstream_closed"],
                        "note": payload["note"],
                    }),
                ),
                Err(error) => share_write_error(&error),
            }
        }
        _ => ConsoleError::unknown_path().response(),
    }
}

/// `_console_display_grant_payload`: bind a one-time grant to the
/// management subject that proved itself here. A bare loopback request is not
/// an administrator, so an SSH `-L` client cannot mint a display grant.
fn handle_display_grant(state: &AppState, request: &Request) -> Response {
    if let Some(reason) = display_unavailable_reason(state) {
        return ConsoleError::new(503, json!({"error": reason})).response();
    }
    let Some(origin) = request_origin(state, request) else {
        return ConsoleError::new(403, json!({"error": "display_authorization_required"}))
            .response();
    };
    let Some(subject) = display_subject(state, request, &origin) else {
        return ConsoleError::new(403, json!({"error": "display_authorization_required"}))
            .response();
    };
    match state.display_grants.issue(subject, &origin) {
        Ok((grant, expires_at)) => send_json(
            200,
            &json!({
                "grant": grant,
                "expires_at": expires_at,
                "origin": origin,
                "url": format!("/display/grant#grant={grant}"),
            }),
        ),
        Err(error) => ConsoleError::new(503, json!({"error": error.code})).response(),
    }
}

/// `_console_display_operator_payload`: acquire or explicitly take over the
/// single-page operator for a live display session.
fn handle_display_operator(state: &AppState, request: &Request) -> Response {
    if let Some(reason) = display_unavailable_reason(state) {
        return ConsoleError::new(503, json!({"error": reason})).response();
    }
    let body = match read_console_body(request) {
        Ok(body) => body,
        Err(error) => return error.response(),
    };
    if let Err(error) = reject_unknown_fields(&body, &["action"]) {
        return error.response();
    }
    let action = match body.get("action").and_then(Value::as_str) {
        Some(action) if action == "acquire" || action == "takeover" => action,
        _ => {
            return ConsoleError::new(400, json!({"error": "display_operator_action_invalid"}))
                .response();
        }
    };
    let Some(origin) = request_origin(state, request) else {
        return ConsoleError::new(403, json!({"error": "display_authorization_required"}))
            .response();
    };
    if has_proxy_metadata(&request.headers) {
        // No Tailscale Serve policy is assembled here, so proxy metadata is
        // never trusted as an identity (fail closed).
        return ConsoleError::new(403, json!({"error": "display_authorization_required"}))
            .response();
    }
    let session = request
        .headers
        .get("Cookie")
        .and_then(|header| crate::session::cookie_value(header, DISPLAY_SESSION_COOKIE));
    let result = state
        .display_grants
        .acquire_operator(session.as_deref(), &origin, action);
    if result["acquired"].as_bool() == Some(true) {
        return send_json(
            200,
            &json!({
                "acquired": true,
                "generation": result["generation"],
                "took_over": result["took_over"].as_bool().unwrap_or(false),
            }),
        );
    }
    match result["error"].as_str() {
        Some("display_operator_exists") => send_json(
            409,
            &json!({
                "error": "display_operator_exists",
                "generation": result["generation"],
                "owner_label": result["owner_label"],
            }),
        ),
        Some("display_session_required") => {
            send_json(409, &json!({"error": "display_session_required"}))
        }
        Some("display_operator_action_invalid") => {
            send_json(400, &json!({"error": "display_operator_action_invalid"}))
        }
        _ => send_json(409, &json!({"error": "display_operator_unavailable"})),
    }
}

/// `_console_display_availability` for the write face: `None` is an
/// unassembled relay, an unavailable fact keeps its exact reason.
fn display_unavailable_reason(state: &AppState) -> Option<String> {
    match state.share_seams.display_availability() {
        None => Some("display_proxy_unavailable".to_string()),
        Some(availability) if availability["available"].as_bool() == Some(true) => None,
        Some(availability) => Some(
            availability["reason"]
                .as_str()
                .filter(|reason| !reason.is_empty())
                .unwrap_or("display_disabled")
                .to_string(),
        ),
    }
}

/// The accepted console origin for a grant, or `None` (fail closed).
fn request_origin(state: &AppState, request: &Request) -> Option<String> {
    let host = request.headers.get("Host")?;
    if !host_allowed(host) || !origin_allowed(request.headers.get("Origin"), state.config.port) {
        return None;
    }
    Some(format!("http://{host}"))
}

/// `display_request_subject`: only the admin control token, the usage session
/// or an existing display session can bind a grant.
fn display_subject(state: &AppState, request: &Request, origin: &str) -> Option<&'static str> {
    if has_proxy_metadata(&request.headers) {
        return None;
    }
    if control_token_ok(
        state.config.control_token_file.as_deref(),
        request.headers.get("X-Admin-Control"),
    ) {
        return Some("control-token");
    }
    let cookie = request.headers.get("Cookie");
    if state.session.valid(cookie) {
        return Some("usage-session");
    }
    let display_cookie =
        cookie.and_then(|header| crate::session::cookie_value(header, DISPLAY_SESSION_COOKIE));
    if state
        .display_grants
        .validate_session(display_cookie.as_deref().unwrap_or_default(), origin)
        .is_some()
    {
        return Some("display-session");
    }
    None
}

/// `console/tailnet.py::has_proxy_metadata`: any Tailscale/forwarding header
/// means the request claims a proxy identity this candidate does not assemble.
fn has_proxy_metadata(headers: &Headers) -> bool {
    headers.iter().any(|header| {
        let name = header.name.to_ascii_lowercase();
        name.starts_with("tailscale-") || name.starts_with("x-forwarded-") || name == "forwarded"
    })
}

fn share_write_error(error: &ShareWriteError) -> Response {
    let mut response =
        ConsoleError::new(error.status(), json!({"error": error.message})).response();
    response.close = true;
    response
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
        let manager = match state.operation_manager.get() {
            Ok(manager) => manager,
            Err(error) => return error.response(),
        };
        let action = match body.get("action").and_then(Value::as_str) {
            Some(action) if (1..=64).contains(&action.chars().count()) => action.to_string(),
            _ => return ConsoleError::body_open(400, "invalid action").response(),
        };
        let target = match body.get("target") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(target)) if target.chars().count() <= 64 => target.clone(),
            _ => return ConsoleError::body_open(400, "invalid target").response(),
        };
        let idempotency_key = match body.get("idempotency_key") {
            None | Some(Value::Null) => None,
            Some(Value::String(key)) if (1..=128).contains(&key.chars().count()) => {
                Some(key.as_str())
            }
            _ => {
                return ConsoleError::body_open(400, "invalid idempotency_key").response();
            }
        };
        let params = match body.get("params") {
            None | Some(Value::Null) => None,
            Some(Value::Object(params)) => Some(params),
            _ => return ConsoleError::body_open(400, "invalid_params").response(),
        };
        match manager.submit(&action, &target, params, idempotency_key) {
            Ok((deduplicated, operation)) => send_json(
                if deduplicated { 200 } else { 201 },
                &json!({"operation": operation}),
            ),
            Err(error) => operation_error_response(&error),
        }
    } else if method == "POST" && rest.len() == 3 && rest[0] == "operations" && rest[2] == "cancel"
    {
        let manager = match state.operation_manager.get() {
            Ok(manager) => manager,
            Err(error) => return error.response(),
        };
        if rest[1].is_empty() || rest[1].chars().count() > 64 {
            return ConsoleError::body_open(400, "invalid operation id").response();
        }
        match manager.cancel(rest[1]) {
            Ok(operation) => send_json(200, &json!({"requested": true, "operation": operation})),
            Err(error) => operation_error_response(&error),
        }
    } else {
        ConsoleError::unknown_path().response()
    }
}

/// `usagesrv.py::_console_error_status` + `_console_error_detail`.
fn operation_error_response(error: &OperationError) -> Response {
    let status = match error.code.as_str() {
        "invalid_action" => 404,
        "action_disabled" => 403,
        "invalid_params" | "invalid_target" | "invalid_idempotency_key" | "invalid_resource" => 400,
        "idempotency_key_conflict" => 409,
        "operation_not_found" => 404,
        "operation_already_finished" => 409,
        _ => 500,
    };
    let code = if status == 500 {
        "console_error".to_string()
    } else {
        error.code.clone()
    };
    let mut payload = json!({"error": code});
    if let Some(reason) = &error.reason {
        payload["reason"] = json!(reason);
    }
    if matches!(
        error.code.as_str(),
        "invalid_params" | "invalid_target" | "invalid_idempotency_key" | "invalid_resource"
    ) {
        if let Some(detail) = safe_detail(&error.message) {
            payload["detail"] = json!(detail);
        }
    }
    ConsoleError::new(status, payload).response()
}

/// The Python `_console_error_detail` shape check: single line, no path
/// separators/drive letters/long token that could be a secret or internal id.
fn safe_detail(message: &str) -> Option<String> {
    let text = message.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.is_empty() {
        return None;
    }
    if text.contains('/') || text.contains('\\') {
        return None;
    }
    let bytes = text.as_bytes();
    let mut run = 0usize;
    for index in 0..bytes.len() {
        let byte = bytes[index];
        if byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.' || byte == b'-' {
            run += 1;
            if run >= 24 {
                return None;
            }
        } else {
            run = 0;
        }
        if index + 1 < bytes.len()
            && byte.is_ascii_alphabetic()
            && bytes[index + 1] == b':'
            && (index == 0 || !bytes[index - 1].is_ascii_alphanumeric())
        {
            return None;
        }
    }
    Some(text.chars().take(200).collect())
}

fn gateway_connection_response(state: &AppState, raw_target: &str) -> Response {
    let service = query_value(raw_target, "service").unwrap_or_default();
    if service != gateway_connection::SERVICE {
        return ConsoleError::new(
            400,
            json!({
                "error": "invalid_service",
                "detail": "service 必须是 chat2api",
            }),
        )
        .response();
    }
    let row = match state.runtime.snapshot() {
        Ok(snapshot) => Some(json!({
            "key": "chat2api",
            "display_name": "chat2api",
            "port": state.config.gateway_port,
            "state": snapshot.display_state(),
            "healthy": snapshot.healthy,
            "detail": snapshot.detail,
            "process_ids": snapshot.process_ids,
        })),
        Err(_) => None,
    };
    match gateway_connection::connection_info(&service, row.as_ref(), epoch_seconds()) {
        Ok(payload) => send_json(200, &payload),
        Err(_) => ConsoleError::new(
            503,
            json!({
                "error": "connection_info_unavailable",
                "detail": "连接信息读取失败：GatewayConnectionError；按未知处理，不返回猜测值",
            }),
        )
        .response(),
    }
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
    if state.config.role == Role::Local {
        let (service_state, http_status, note) = match snapshot.state.as_str() {
            "running" => ("listening", Value::Null, "TCP 端口可达（未做 HTTP 探测）"),
            "unknown" => ("unknown", Value::Null, "TCP 连接超时（探测未定，非未监听）"),
            _ => ("not_listening", Value::Null, "loopback 端口未监听"),
        };
        return send_json(
            200,
            &json!({
                "checked_at": epoch_seconds(),
                "cached": false,
                "services": [
                    {"id": "chat2api", "port": state.config.gateway_port,
                     "state": service_state, "http_status": http_status, "note": note},
                    {"id": "usagesrv", "port": state.config.port,
                     "state": "listening", "note": "console host itself"},
                ],
            }),
        );
    }
    let mut services = vec![service_row(state, &snapshot)];
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
        "services": [service_row(state, snapshot)],
        "meta": {
            "source": if state.config.role == Role::Local {
                "local-runtime"
            } else {
                "linux-pid-file"
            },
            "generated_epoch": epoch_seconds(),
        },
    })
}

fn service_row(state: &AppState, snapshot: &RuntimeSnapshot) -> Value {
    if state.config.role == Role::Local {
        return json!({
            "key": "chat2api",
            "display_name": "chat2api",
            "port": state.config.gateway_port,
            "state": snapshot.display_state(),
            "healthy": snapshot.healthy,
            "detail": snapshot.detail,
            "process_ids": snapshot.process_ids,
        });
    }
    json!({
        "key": "share-gateway",
        "display_name": "UM 分享入口 8100",
        "port": state.config.gateway_port,
        "state": snapshot.display_state(),
        "healthy": snapshot.healthy,
        "detail": snapshot.detail,
        "process_ids": snapshot.process_ids,
    })
}

fn send_json(status: u16, payload: &Value) -> Response {
    Response::json(status, payload).with_header("Cache-Control", "no-store")
}

/// Python's `ShareManager.status()` keeps `_last_failure` in-process; the read
/// store derives it from the durable publish journal. Prefer this process's value
/// when it performed the failed publication, so a later success retains it exactly
/// like Python and a failed journal update cannot hide the failure.
fn with_last_publication_failure(state: &AppState, mut payload: Value) -> Value {
    if let Some(failure) = state.share_write.last_failure() {
        if let Some(object) = payload.as_object_mut() {
            object.insert("last_publication_failure".to_string(), failure);
        }
    }
    payload
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

/// The share-usage ledger path, or `None` when the configured backend dir is
/// absent/relative (never a guessed path).
fn share_ledger_path(state: &AppState) -> Option<std::path::PathBuf> {
    let dir = state.config.share_backend_dir.as_ref()?;
    if !dir.is_absolute() {
        return None;
    }
    Some(dir.join("usage-share-backend.jsonl"))
}

fn share_usage_response(state: &AppState, raw_target: &str, requests: bool) -> Response {
    let Some(ledger) = share_ledger_path(state) else {
        return ConsoleError::new(503, json!({"error": "share_ledger_path_unresolved"})).response();
    };
    let query = query_values(raw_target);
    let allowed: &[&str] = if requests {
        &["window", "key", "model", "result", "cursor", "limit"]
    } else {
        &["window", "key", "model", "result"]
    };
    if let Some(name) = query.keys().find(|name| !allowed.contains(&name.as_str())) {
        let _ = name;
        return ConsoleError::new(
            400,
            json!({
                "error": "invalid_share_usage_query",
                "reason": "unknown_query_field",
            }),
        )
        .response();
    }
    if let Some(name) = query
        .iter()
        .find(|(_, values)| values.len() != 1)
        .map(|(name, _)| name)
    {
        let _ = name;
        return ConsoleError::new(
            400,
            json!({
                "error": "invalid_share_usage_query",
                "reason": "duplicate_query_field",
            }),
        )
        .response();
    }
    let value = |name: &str| query.get(name).and_then(|values| values.first()).cloned();
    let filters = UsageFilters {
        window: value("window"),
        key_id: value("key"),
        model: value("model"),
        result: value("result"),
    };
    let limit = if requests {
        match value("limit") {
            None => metering::SHARE_REQUEST_PAGE_DEFAULT,
            Some(raw) => {
                if !raw.bytes().all(|byte| byte.is_ascii_digit()) {
                    return ConsoleError::new(
                        400,
                        json!({
                            "error": "invalid_share_usage_query",
                            "reason": "limit",
                        }),
                    )
                    .response();
                }
                let parsed = raw.parse::<i64>().unwrap_or_default();
                if !(1..=metering::SHARE_REQUEST_PAGE_MAX).contains(&parsed) {
                    return ConsoleError::new(
                        400,
                        json!({
                            "error": "invalid_share_usage_query",
                            "reason": "limit",
                        }),
                    )
                    .response();
                }
                parsed
            }
        }
    } else {
        0
    };
    let cursor = value("cursor");
    let result = if requests {
        metering::read_share_requests(&ledger, &filters, cursor.as_deref(), limit, None)
    } else {
        metering::read_share_usage(&ledger, &filters, metering::DEFAULT_LIMIT, None)
    };
    match result {
        Ok(mut payload) => {
            metering::attach_key_labels(
                &mut payload,
                if requests { "rows" } else { "keys" },
                if requests { "keyId" } else { "key_id" },
                state.config.share_store_file.as_ref(),
            );
            send_json(200, &payload)
        }
        Err(_) => ConsoleError::new(400, json!({"error": "invalid_share_usage_query"})).response(),
    }
}

/// All query-string values grouped by name, Python `parse_qs` semantics.
fn query_values(raw_target: &str) -> std::collections::BTreeMap<String, Vec<String>> {
    let mut values = std::collections::BTreeMap::new();
    let Some(query) = raw_target.split_once('?').map(|(_, query)| query) else {
        return values;
    };
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let key = decode_query_part(key);
        if key.is_empty() {
            continue;
        }
        values
            .entry(key)
            .or_insert_with(Vec::new)
            .push(decode_query_part(value));
    }
    values
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
