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
use crate::cloud_ops::cloud_ops_status;
use crate::config::{ConfigError, ConsoleConfig, Role};
use crate::display::{display_session, display_state};
use crate::error::ConsoleError;
use crate::metering::{self, UsageFilters};
use crate::operation_manager::{OperationError, OperationManagerSlot, UnwiredActionExecutor};
use crate::operations::OperationsReader;
use crate::runtime::{LinuxPidFileFacts, RuntimeFacts, RuntimeSnapshot};
use crate::share_auth::{control_token_ok, management_auth, SessionState};
use crate::share_read::{ShareReadSeams, ShareReadStore, UnwiredSeams};
use crate::share_write::{
    console_public_row, ShareWriteError, ShareWriteSeams, ShareWriteStore, UnwiredWriteSeams,
};
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
        config.validate_candidate().map_err(ServerError::Config)?;
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
            share_write: ShareWriteStore::new(
                config.share_store_file.clone().unwrap_or_default(),
                config.share_secret_key_file.clone(),
                write_seams,
            ),
            session: SessionState::new(),
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
    if path.starts_with("/api/share/") {
        return handle_share_read(state, request, &target);
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
        "POST" | "PATCH" | "DELETE" if rest.first() == Some(&"share-management") => {
            handle_share_management_write(state, method, &rest[1..], request)
        }
        "POST" | "PATCH" | "DELETE" => handle_console_write(state, method, &rest, request),
        _ => ConsoleError::unknown_path().response(),
    }
}

fn handle_share_read(state: &AppState, request: &Request, raw_target: &str) -> Response {
    if let Err(error) = reject_duplicate_sensitive_header(&request.headers) {
        return error.response();
    }
    let authorized = management_auth(
        &state.session,
        state.config.control_token_file.as_deref(),
        request.headers.get("Cookie"),
        request.headers.get("X-Admin-Control"),
    );
    if !authorized {
        let mut response = ConsoleError::new(
            401,
            json!({"error": "share management credentials required"}),
        )
        .response();
        response.close = true;
        return response;
    }
    let path = raw_target.split('?').next().unwrap_or(raw_target);
    let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    let rest: Vec<&str> = segments.iter().skip(2).copied().collect();
    if request.method.as_str() != "GET" {
        // Writes belong to S6; the candidate never fakes a mutation.
        return ConsoleError::not_implemented("share_write_s6").response();
    }
    match rest.as_slice() {
        ["usage"] => share_usage_response(state, raw_target, false),
        ["usage", "requests"] => share_usage_response(state, raw_target, true),
        ["status"] => match state.share.raw_status(state.share_seams.broker_snapshot()) {
            Ok(payload) => send_json(200, &payload),
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
        ["share-usage"] => share_usage_response(state, raw_target, false),
        ["share-usage", "requests"] => share_usage_response(state, raw_target, true),
        ["cloud-ops"] => send_json(
            200,
            &cloud_ops_status(state.config.security_state_file.as_deref()),
        ),
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
