//! Local model API ingress (`--mode ingress`) and worker port file (`--mode worker`).
//!
//! The local 8000 process is split into two processes: the ingress owns the
//! client-facing `127.0.0.1:8000` listener, and the worker is a normal
//! [`GatewayServer`](crate::GatewayServer) bound to an ephemeral loopback port
//! that writes its actual port to `--worker-port-file`. While the worker is down,
//! the ingress keeps listening and queues only requests that were never forwarded;
//! each queued request is forwarded exactly once after the worker becomes
//! connectable.
//!
//! The ingress forwards raw HTTP/1.1 bytes to the worker with `Connection: close`
//! and relays the worker response without buffering the whole body. A response with
//! `Content-Length` or `Transfer-Encoding: chunked` keeps the client connection
//! alive; a close-delimited response closes it.

use std::fs;
use std::io::{self, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::config::{is_loopback_host, ConfigError};
use crate::error::{ErrorProtocol, GatewayError};
use crate::event_log::EventLog;
use crate::gate::constant_time_eq;
use crate::http::{
    accept_connections, random_hex, read_request_body, read_request_head, write_response,
    write_response_head, BodyRead, Headers, Request, Response, MAX_HEADER_BYTES,
};
use crate::server::{
    error_protocol, is_local_control_request, with_error_headers, write_memory_pressure,
    ServerError, ServerHandle,
};
use crate::GatewayServer;

/// The default worker port file, relative to the repository root.
pub const DEFAULT_WORKER_PORT_FILE: &str = "data/chat2api-worker.port";
/// The desired-state file path used when `--desired-state-file` is omitted.
pub const DESIRED_STATE_ENV: &str = "SELFUSEAI_GATEWAY_DESIRED_STATE";
/// The boot identity used when `--boot-id` is omitted.
pub const BOOT_ID_ENV: &str = "SELFUSEAI_GATEWAY_BOOT_ID";
/// The default worker recovery wait, matching ADR 0014's three minutes.
pub const DEFAULT_RECOVERY_WAIT_SECONDS: u64 = 180;
/// The default queued request count.
pub const DEFAULT_RECOVERY_QUEUE_MAX: usize = 64;
/// The default queued body bytes (64 MiB).
pub const DEFAULT_RECOVERY_QUEUE_MAX_BYTES: u64 = 64 * 1024 * 1024;
const DEFAULT_RECOVERY_POLL_MILLIS: u64 = 50;
const DEFAULT_CONNECT_TIMEOUT_MILLIS: u64 = 200;
const DEFAULT_SHUTDOWN_WAIT_SECONDS: u64 = 30;
const HEAD_READ_TIMEOUT: Duration = Duration::from_secs(30);
const WORKER_READ_TIMEOUT: Duration = Duration::from_secs(300);

/// The ingress process configuration.
#[derive(Debug, Clone)]
pub struct IngressConfig {
    pub bind_host: String,
    pub bind_port: u16,
    pub worker_port_file: PathBuf,
    /// The supervisor's desired-state JSON; `None` reads the environment in
    /// `main` and stays unknown here.
    pub desired_state_file: Option<PathBuf>,
    /// The boot identity the desired-state file must carry.
    pub boot_id: Option<String>,
    /// How long a never-forwarded request may wait for the worker.
    pub recovery_wait: Duration,
    /// Maximum queued request count.
    pub queue_max: usize,
    /// Maximum buffered body bytes across the queue.
    pub queue_max_bytes: u64,
    /// The local control token file, when the ingress validates shutdown itself.
    pub control_token_file: Option<PathBuf>,
    /// Append listener diagnostics to the local gateway service log.
    pub service_log: Option<PathBuf>,
    pub poll_interval: Duration,
    pub connect_timeout: Duration,
    /// How long the ingress waits for the worker to exit after forwarding a
    /// shutdown.
    pub shutdown_wait: Duration,
    /// The request body read cap before forwarding.
    pub max_request_bytes: u64,
    pub head_read_timeout: Duration,
    pub body_read_timeout: Duration,
    pub worker_read_timeout: Duration,
}

impl Default for IngressConfig {
    fn default() -> Self {
        Self {
            bind_host: "127.0.0.1".to_string(),
            bind_port: 0,
            worker_port_file: PathBuf::from(DEFAULT_WORKER_PORT_FILE),
            desired_state_file: None,
            boot_id: None,
            recovery_wait: Duration::from_secs(DEFAULT_RECOVERY_WAIT_SECONDS),
            queue_max: DEFAULT_RECOVERY_QUEUE_MAX,
            queue_max_bytes: DEFAULT_RECOVERY_QUEUE_MAX_BYTES,
            control_token_file: None,
            service_log: None,
            poll_interval: Duration::from_millis(DEFAULT_RECOVERY_POLL_MILLIS),
            connect_timeout: Duration::from_millis(DEFAULT_CONNECT_TIMEOUT_MILLIS),
            shutdown_wait: Duration::from_secs(DEFAULT_SHUTDOWN_WAIT_SECONDS),
            max_request_bytes: crate::config::DEFAULT_SHARE_MAX_REQUEST_BYTES,
            head_read_timeout: HEAD_READ_TIMEOUT,
            body_read_timeout: Duration::from_secs(
                crate::config::DEFAULT_BODY_READ_TIMEOUT_SECONDS,
            ),
            worker_read_timeout: WORKER_READ_TIMEOUT,
        }
    }
}

impl IngressConfig {
    /// Fail closed before binding the ingress listener.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !is_loopback_host(&self.bind_host) {
            return Err(ConfigError(format!(
                "ingress bind host is not loopback: {}",
                self.bind_host
            )));
        }
        if self.bind_port != 0 && self.bind_port != 8000 {
            return Err(ConfigError(format!(
                "ingress must bind 8000 or an ephemeral loopback port, got {}",
                self.bind_port
            )));
        }
        if self.queue_max == 0 {
            return Err(ConfigError(
                "ingress recovery queue count must be at least 1".to_string(),
            ));
        }
        if self.queue_max_bytes == 0 {
            return Err(ConfigError(
                "ingress recovery queue bytes must be at least 1".to_string(),
            ));
        }
        if self.max_request_bytes == 0 {
            return Err(ConfigError(
                "ingress request body limit must be at least 1".to_string(),
            ));
        }
        Ok(())
    }
}

/// Write a worker port file atomically: a temporary file plus rename.
///
/// The worker is the only writer, and the ingress never guesses a port. A reader
/// either sees the previous complete file or the new complete file, never a partial
/// decimal. The second line carries the worker PID so the ingress can reject a
/// record whose owner is no longer the gateway binary.
pub fn write_worker_port_file(path: &Path, port: u16) -> io::Result<()> {
    let Some(parent) = path.parent() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "worker port file must have a parent directory",
        ));
    };
    if !parent.as_os_str().is_empty() {
        fs::create_dir_all(parent)?;
    }
    let file_name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "worker port file has no name"))?
        .to_string_lossy()
        .into_owned();
    let temp = path.with_file_name(format!(".{file_name}.tmp-{}", std::process::id()));
    let result = (|| {
        let mut file = fs::File::create(&temp)?;
        file.write_all(format!("{port}\n{}\n", std::process::id()).as_bytes())?;
        file.sync_all()?;
        fs::rename(&temp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

/// Remove the worker port file on a graceful worker exit.
pub fn remove_worker_port_file(path: &Path) {
    let _ = fs::remove_file(path);
}

/// Start a gateway server as the local worker and publish its actual port.
///
/// The port file is written only after the listener is bound, so the ingress never
/// sees a port that cannot be connected yet.
pub fn start_worker(
    config: crate::GatewayConfig,
    port_file: &Path,
) -> Result<ServerHandle, ServerError> {
    let server = GatewayServer::start(config)?;
    if let Err(error) = write_worker_port_file(port_file, server.addr().port()) {
        let mut server = server;
        server.shutdown();
        return Err(ServerError::Io(error));
    }
    Ok(server)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DesiredState {
    Running,
    Stopped,
    Unknown,
}

struct QueuedRequest {
    method: String,
    target: String,
    headers: Headers,
    body: Vec<u8>,
    request_id: String,
}

impl QueuedRequest {
    fn new(request: &Request, body: Vec<u8>) -> Self {
        Self {
            method: request.method.clone(),
            target: request.target.clone(),
            headers: request.headers.clone(),
            body,
            request_id: request.request_id.clone(),
        }
    }

    fn path(&self) -> &str {
        self.target.split('?').next().unwrap_or(&self.target)
    }
}

struct QueuedEntry {
    id: u64,
    request: Arc<QueuedRequest>,
}

#[derive(Default)]
struct QueueState {
    entries: Vec<QueuedEntry>,
    bytes: u64,
    next_id: u64,
}

/// The state shared by every ingress connection.
pub struct IngressState {
    pub config: IngressConfig,
    queue: Mutex<QueueState>,
    stopping: AtomicBool,
    shutdown: Arc<AtomicBool>,
    shutdown_request: Arc<AtomicBool>,
    control_token: Option<String>,
    enqueued_total: std::sync::atomic::AtomicUsize,
}

impl IngressState {
    /// The number of requests currently waiting for the worker.
    pub fn queued_requests(&self) -> usize {
        self.lock_queue().entries.len()
    }

    /// The buffered body bytes currently waiting for the worker.
    pub fn queued_bytes(&self) -> u64 {
        self.lock_queue().bytes
    }

    /// The total number of requests ever put on the recovery queue.
    pub fn enqueued_total(&self) -> usize {
        self.enqueued_total.load(Ordering::SeqCst)
    }

    /// Whether `/admin/shutdown` asked the ingress to stop.
    pub fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    fn lock_queue(&self) -> std::sync::MutexGuard<'_, QueueState> {
        self.queue.lock().unwrap_or_else(|error| error.into_inner())
    }

    fn stop_requested(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
            || self.shutdown_request.load(Ordering::SeqCst)
            || self.shutdown.load(Ordering::SeqCst)
    }
}

/// A started ingress listener.
pub struct IngressHandle {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    state: Arc<IngressState>,
}

impl std::fmt::Debug for IngressHandle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("IngressHandle")
            .field("addr", &self.addr)
            .finish_non_exhaustive()
    }
}

impl IngressHandle {
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn state(&self) -> Arc<IngressState> {
        Arc::clone(&self.state)
    }

    /// Set by `/admin/shutdown`; the process entry waits on it and then exits.
    pub fn shutdown_request(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.state.shutdown_request)
    }

    pub fn shutdown(&mut self) {
        if let Some(handle) = self.handle.take() {
            self.shutdown.store(true, Ordering::SeqCst);
            self.state.stopping.store(true, Ordering::SeqCst);
            let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
            let _ = handle.join();
        }
    }
}

impl Drop for IngressHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// The ingress listener factory.
pub struct IngressServer;

impl IngressServer {
    /// Bind the ingress listener and start accepting connections.
    pub fn start(config: IngressConfig) -> Result<IngressHandle, ServerError> {
        config.validate().map_err(ServerError::Config)?;
        let control_token = match &config.control_token_file {
            Some(path) => Some(
                fs::read_to_string(path)
                    .map_err(ServerError::Io)?
                    .trim()
                    .to_string(),
            )
            .filter(|token| !token.is_empty()),
            None => None,
        };
        let listener = TcpListener::bind((config.bind_host.as_str(), config.bind_port))
            .map_err(ServerError::Io)?;
        listener.set_nonblocking(true).map_err(ServerError::Io)?;
        let addr = listener.local_addr().map_err(ServerError::Io)?;
        let shutdown = Arc::new(AtomicBool::new(false));
        let event_log = EventLog::new(config.service_log.clone());
        let state = Arc::new(IngressState {
            config,
            queue: Mutex::new(QueueState::default()),
            stopping: AtomicBool::new(false),
            shutdown: Arc::clone(&shutdown),
            shutdown_request: Arc::new(AtomicBool::new(false)),
            control_token,
            enqueued_total: std::sync::atomic::AtomicUsize::new(0),
        });
        let worker_state = Arc::clone(&state);
        let worker_shutdown = Arc::clone(&shutdown);
        let handle = thread::Builder::new()
            .spawn(move || {
                accept_connections(
                    &worker_shutdown,
                    || listener.accept(),
                    |stream, peer| {
                        let state = Arc::clone(&worker_state);
                        thread::Builder::new()
                            .spawn(move || handle_connection(state, stream, peer.ip()))?;
                        Ok(())
                    },
                    |operation, error| event_log.listener_failure("ingress", operation, error),
                );
            })
            .map_err(ServerError::Io)?;
        Ok(IngressHandle {
            addr,
            shutdown,
            handle: Some(handle),
            state,
        })
    }
}

fn handle_connection(state: Arc<IngressState>, mut stream: TcpStream, peer_ip: IpAddr) {
    // Accepted sockets inherit the non-blocking listener on Windows.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(state.config.head_read_timeout));
    loop {
        let head = match read_request_head(&mut stream, peer_ip) {
            Ok(Some(head)) => head,
            Ok(None) => return,
            Err(_) => {
                let request_id = random_hex(16);
                let _ = write_error(
                    &mut stream,
                    GatewayError::invalid_request("Invalid request line or headers"),
                    ErrorProtocol::OpenAi,
                    &request_id,
                );
                return;
            }
        };
        let request_id = head.request_id.clone();
        if head.headers.get_all("Content-Length").len() > 1 || head.headers.has("Transfer-Encoding")
        {
            let _ = write_error(
                &mut stream,
                GatewayError::invalid_request("Invalid request framing"),
                error_protocol(&head.path()),
                &request_id,
            );
            return;
        }
        // A stopped or unknown intent is answered before the body is read, so
        // an explicit stop takes effect without waiting for a large upload.
        // Only model API paths enter the recovery queue; `/admin/*`, `/` and
        // the health routes keep their immediate answer.
        let queueable_path = head.path().starts_with("/v1/") || head.path().starts_with("/api/");
        if queueable_path {
            match desired_state(&state) {
                DesiredState::Stopped => {
                    let _ = write_error(
                        &mut stream,
                        GatewayError::gateway_stopped(),
                        error_protocol(&head.path()),
                        &request_id,
                    );
                    return;
                }
                DesiredState::Unknown => {
                    let _ = write_error(
                        &mut stream,
                        GatewayError::gateway_not_running(),
                        error_protocol(&head.path()),
                        &request_id,
                    );
                    return;
                }
                DesiredState::Running => {}
            }
            if state.stop_requested() {
                let _ = write_error(
                    &mut stream,
                    GatewayError::gateway_stopped(),
                    error_protocol(&head.path()),
                    &request_id,
                );
                return;
            }
        }
        if head
            .headers
            .get("Expect")
            .is_some_and(|value| value.eq_ignore_ascii_case("100-continue"))
        {
            if stream.write_all(b"HTTP/1.1 100 Continue\r\n\r\n").is_err() {
                return;
            }
            let _ = stream.flush();
        }
        let protocol = error_protocol(head.path());
        let _ = stream.set_read_timeout(Some(state.config.body_read_timeout));
        let body_result = read_request_body(&mut stream, head, state.config.max_request_bytes);
        let _ = stream.set_read_timeout(Some(state.config.head_read_timeout));
        let request = match body_result {
            Ok((request, _trailing)) => request,
            Err(error) if error.kind() == io::ErrorKind::OutOfMemory => {
                let _ = write_memory_pressure(&mut stream, protocol, &request_id);
                return;
            }
            Err(error) if is_timeout_error(&error) => {
                let _ = write_error(
                    &mut stream,
                    GatewayError::request_timeout(),
                    ErrorProtocol::OpenAi,
                    &request_id,
                );
                return;
            }
            Err(_) => {
                let _ = write_error(
                    &mut stream,
                    GatewayError::invalid_request("Invalid request line or headers"),
                    ErrorProtocol::OpenAi,
                    &request_id,
                );
                return;
            }
        };
        if let BodyRead::InvalidLength = &request.body {
            let _ = write_error(
                &mut stream,
                GatewayError::invalid_request("Invalid Content-Length header"),
                error_protocol(request.path()),
                &request.request_id,
            );
            return;
        }
        if matches!(
            &request.body,
            BodyRead::NegativeLength | BodyRead::TooLarge(_)
        ) {
            let _ = write_error(
                &mut stream,
                GatewayError::payload_too_large(),
                error_protocol(request.path()),
                &request.request_id,
            );
            return;
        }
        if let BodyRead::Incomplete = &request.body {
            let _ = write_error(
                &mut stream,
                GatewayError::invalid_request("Incomplete request body"),
                error_protocol(request.path()),
                &request.request_id,
            );
            return;
        }
        let close = if request.method == "POST" && request.path() == "/admin/shutdown" {
            handle_admin_shutdown(&state, &mut stream, request)
        } else if request.method == "GET" && matches!(request.path(), "/health" | "/ready") {
            handle_health(&state, &mut stream, request)
        } else {
            handle_forward(&state, &mut stream, request)
        };
        match close {
            Ok(true) => return,
            Ok(false) => {}
            Err(_) => return,
        }
    }
}

fn handle_forward(
    state: &IngressState,
    client: &mut TcpStream,
    request: Request,
) -> io::Result<bool> {
    let protocol = error_protocol(request.path());
    let request_id = request.request_id.clone();
    let Some(body) = body_bytes(&request) else {
        return write_error(
            client,
            GatewayError::invalid_request("Incomplete request body"),
            protocol,
            &request_id,
        );
    };
    let queued = QueuedRequest::new(&request, body.to_vec());
    let queueable = request.path().starts_with("/v1/") || request.path().starts_with("/api/");
    if queueable {
        match desired_state(state) {
            DesiredState::Stopped => {
                return write_error(
                    client,
                    GatewayError::gateway_stopped(),
                    protocol,
                    &request_id,
                )
            }
            DesiredState::Unknown => {
                return write_error(
                    client,
                    GatewayError::gateway_not_running(),
                    protocol,
                    &request_id,
                )
            }
            DesiredState::Running => {}
        }
        if state.stop_requested() {
            return write_error(
                client,
                GatewayError::gateway_stopped(),
                protocol,
                &request_id,
            );
        }
    }
    if let Some((mut worker, worker_port)) = try_connect_worker(&state.config) {
        let outcome = forward_to_worker(client, &mut worker, &queued, worker_port)?;
        return Ok(outcome.close);
    }
    if !queueable {
        // Console reads and other non-model routes never wait for the worker;
        // only model API requests enter the bounded recovery queue.
        return write_error(
            client,
            GatewayError::gateway_not_running(),
            protocol,
            &request_id,
        );
    }
    let id = match enqueue(state, queued) {
        Ok(id) => id,
        Err(error) => return write_error(client, error, protocol, &request_id),
    };
    match wait_for_worker(state, id, client) {
        WaitOutcome::Ready(mut worker, worker_port, queued) => {
            let outcome = forward_to_worker(client, &mut worker, &queued, worker_port)?;
            Ok(outcome.close)
        }
        WaitOutcome::TimedOut => write_error(
            client,
            GatewayError::gateway_recovery_timeout(),
            protocol,
            &request_id,
        ),
        WaitOutcome::Stopped => write_error(
            client,
            GatewayError::gateway_stopped(),
            protocol,
            &request_id,
        ),
        WaitOutcome::Cancelled => Ok(false),
    }
}

fn handle_health(
    state: &IngressState,
    client: &mut TcpStream,
    request: Request,
) -> io::Result<bool> {
    let protocol = ErrorProtocol::OpenAi;
    let request_id = request.request_id.clone();
    let Some((mut worker, worker_port)) = try_connect_worker(&state.config) else {
        return write_error(
            client,
            GatewayError::gateway_not_running(),
            protocol,
            &request_id,
        );
    };
    let queued = QueuedRequest::new(&request, Vec::new());
    let outcome = forward_to_worker(client, &mut worker, &queued, worker_port)?;
    Ok(outcome.close)
}

fn handle_admin_shutdown(
    state: &IngressState,
    client: &mut TcpStream,
    request: Request,
) -> io::Result<bool> {
    let protocol = ErrorProtocol::OpenAi;
    let request_id = request.request_id.clone();
    if let Some(token) = &state.control_token {
        if !is_local_control_request(
            request.peer_ip,
            request.headers.get("Host").unwrap_or_default(),
            request.headers.get("Origin"),
        ) {
            return write_error(
                client,
                GatewayError::control_denied_endpoint(),
                protocol,
                &request_id,
            );
        }
        let supplied = request.headers.get("X-Admin-Control").unwrap_or_default();
        if !constant_time_eq(supplied.as_bytes(), token.as_bytes()) {
            return write_error(
                client,
                GatewayError::control_denied_token(),
                protocol,
                &request_id,
            );
        }
    }
    let Some(body) = body_bytes(&request) else {
        return write_error(
            client,
            GatewayError::invalid_request("Incomplete request body"),
            protocol,
            &request_id,
        );
    };
    let queued = QueuedRequest::new(&request, body.to_vec());
    state.stopping.store(true, Ordering::SeqCst);
    match try_connect_worker(&state.config) {
        Some((mut worker, worker_port)) => {
            let outcome = match forward_to_worker(client, &mut worker, &queued, worker_port) {
                Ok(outcome) => outcome,
                Err(error) => {
                    state.shutdown_request.store(true, Ordering::SeqCst);
                    return Err(error);
                }
            };
            if outcome.upstream_status == Some(202) {
                wait_for_worker_exit(state);
                state.shutdown_request.store(true, Ordering::SeqCst);
            } else {
                // The worker refused the shutdown; the ingress keeps running.
                state.stopping.store(false, Ordering::SeqCst);
            }
            Ok(true)
        }
        None => {
            let response = Response::json(202, &json!({"status": "stopping"})).closing();
            write_response(client, &response)?;
            state.shutdown_request.store(true, Ordering::SeqCst);
            Ok(true)
        }
    }
}

fn desired_state(state: &IngressState) -> DesiredState {
    let Some(path) = state.config.desired_state_file.as_ref() else {
        return DesiredState::Unknown;
    };
    let Ok(text) = fs::read_to_string(path) else {
        return DesiredState::Unknown;
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return DesiredState::Unknown;
    };
    match value.get("state").and_then(Value::as_str) {
        Some("stopped") => DesiredState::Stopped,
        Some("running") => {
            let boot_id = value
                .get("boot_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            match state.config.boot_id.as_deref() {
                Some(expected) if !expected.is_empty() && boot_id == expected => {
                    DesiredState::Running
                }
                _ => DesiredState::Unknown,
            }
        }
        _ => DesiredState::Unknown,
    }
}

fn enqueue(state: &IngressState, request: QueuedRequest) -> Result<u64, GatewayError> {
    let mut queue = state.lock_queue();
    if state.stop_requested() {
        return Err(GatewayError::gateway_stopped());
    }
    let bytes = request.body.len() as u64;
    if queue.entries.len() >= state.config.queue_max
        || queue.bytes.saturating_add(bytes) > state.config.queue_max_bytes
    {
        return Err(GatewayError::gateway_recovery_queue_full());
    }
    let id = queue.next_id;
    queue.next_id = queue.next_id.wrapping_add(1);
    queue.bytes += bytes;
    queue.entries.push(QueuedEntry {
        id,
        request: Arc::new(request),
    });
    state.enqueued_total.fetch_add(1, Ordering::SeqCst);
    Ok(id)
}

fn take_queued(state: &IngressState, id: u64) -> Option<Arc<QueuedRequest>> {
    let mut queue = state.lock_queue();
    let position = queue.entries.iter().position(|entry| entry.id == id)?;
    let entry = queue.entries.remove(position);
    queue.bytes = queue.bytes.saturating_sub(entry.request.body.len() as u64);
    Some(entry.request)
}

enum WaitOutcome {
    Ready(TcpStream, u16, Arc<QueuedRequest>),
    TimedOut,
    Stopped,
    Cancelled,
}

fn wait_for_worker(state: &IngressState, id: u64, client: &mut TcpStream) -> WaitOutcome {
    let deadline = Instant::now() + state.config.recovery_wait;
    loop {
        if state.stop_requested() {
            let _ = take_queued(state, id);
            return WaitOutcome::Stopped;
        }
        if Instant::now() >= deadline {
            let _ = take_queued(state, id);
            return WaitOutcome::TimedOut;
        }
        if client_disconnected(client) {
            let _ = take_queued(state, id);
            return WaitOutcome::Cancelled;
        }
        if let Some((worker, worker_port)) = try_connect_worker(&state.config) {
            if state.stop_requested() {
                let _ = take_queued(state, id);
                return WaitOutcome::Stopped;
            }
            let Some(request) = take_queued(state, id) else {
                return WaitOutcome::Cancelled;
            };
            return WaitOutcome::Ready(worker, worker_port, request);
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        thread::sleep(state.config.poll_interval.min(remaining));
    }
}

fn try_connect_worker(config: &IngressConfig) -> Option<(TcpStream, u16)> {
    let port = read_worker_port(&config.worker_port_file)?;
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let stream = TcpStream::connect_timeout(&addr, config.connect_timeout).ok()?;
    let _ = stream.set_read_timeout(Some(config.worker_read_timeout));
    let _ = stream.set_write_timeout(Some(config.worker_read_timeout));
    let _ = stream.set_nodelay(true);
    Some((stream, port))
}

fn read_worker_port(path: &Path) -> Option<u16> {
    let text = fs::read_to_string(path).ok()?;
    let mut lines = text.lines();
    let port = lines.next()?.trim().parse::<u16>().ok()?;
    if port == 0 {
        return None;
    }
    // The second line is the worker PID; a missing or invalid line is a stale
    // record (port-only files are no longer accepted). A record whose PID is
    // no longer the gateway binary is stale too: never forward an
    // Authorization-bearing request to whatever now owns that port.
    let pid = lines.next()?.trim().parse::<u32>().ok()?;
    if !worker_identity_matches(pid) {
        return None;
    }
    Some(port)
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
    fn QueryFullProcessImageNameW(
        process: *mut core::ffi::c_void,
        flags: u32,
        name: *mut u16,
        size: *mut u32,
    ) -> i32;
    fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
}

#[cfg(windows)]
fn worker_identity_matches(pid: u32) -> bool {
    let Some(expected) = std::env::current_exe()
        .ok()
        .and_then(|path| path.canonicalize().ok())
    else {
        return false;
    };
    let Some(actual) = process_image_path(pid) else {
        return false;
    };
    let Ok(actual) = actual.canonicalize() else {
        return false;
    };
    actual == expected
}

#[cfg(windows)]
fn process_image_path(pid: u32) -> Option<std::path::PathBuf> {
    use std::os::windows::ffi::OsStringExt;

    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return None;
    }
    let mut buffer = vec![0u16; 32768];
    let mut size = buffer.len() as u32;
    let ok = unsafe { QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut size) };
    unsafe {
        CloseHandle(handle);
    }
    if ok == 0 {
        return None;
    }
    Some(std::path::PathBuf::from(std::ffi::OsString::from_wide(
        &buffer[..size as usize],
    )))
}

#[cfg(not(windows))]
fn worker_identity_matches(_pid: u32) -> bool {
    // Non-Windows ingress skips the image-name check; the record is still a
    // complete atomic port and the port must connect.
    true
}

fn wait_for_worker_exit(state: &IngressState) {
    let deadline = Instant::now() + state.config.shutdown_wait;
    while Instant::now() < deadline {
        if state.shutdown.load(Ordering::SeqCst) {
            return;
        }
        if try_connect_worker(&state.config).is_none() {
            return;
        }
        thread::sleep(state.config.poll_interval);
    }
}

fn client_disconnected(stream: &mut TcpStream) -> bool {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1)));
    let mut byte = [0u8; 1];
    let disconnected = match stream.peek(&mut byte) {
        Ok(0) => true,
        Ok(_) => false,
        Err(error) => matches!(
            error.kind(),
            io::ErrorKind::ConnectionReset
                | io::ErrorKind::ConnectionAborted
                | io::ErrorKind::UnexpectedEof
                | io::ErrorKind::BrokenPipe
                | io::ErrorKind::NotConnected
        ),
    };
    let _ = stream.set_read_timeout(Some(HEAD_READ_TIMEOUT));
    disconnected
}

struct ForwardOutcome {
    close: bool,
    upstream_status: Option<u16>,
}

fn forward_to_worker(
    client: &mut TcpStream,
    worker: &mut TcpStream,
    request: &QueuedRequest,
    worker_port: u16,
) -> io::Result<ForwardOutcome> {
    let bytes = serialize_request(request, worker_port);
    let written = write_request_bytes(worker, &bytes).unwrap_or(0);
    if written < bytes.len() {
        let error = GatewayError::gateway_worker_lost(written > 0);
        let close = write_error(
            client,
            error,
            error_protocol(request.path()),
            &request.request_id,
        )?;
        return Ok(ForwardOutcome {
            close,
            upstream_status: None,
        });
    }
    let head = match read_response_head(worker) {
        Ok(Some(head)) => head,
        Ok(None) | Err(_) => {
            let error = GatewayError::gateway_worker_lost(true);
            let close = write_error(
                client,
                error,
                error_protocol(request.path()),
                &request.request_id,
            )?;
            return Ok(ForwardOutcome {
                close,
                upstream_status: None,
            });
        }
    };
    let status = head.status;
    let framing = response_framing(&head);
    write_client_response_head(client, &head, &framing)?;
    let close = relay_body(client, worker, &framing, head.leftover)?;
    Ok(ForwardOutcome {
        close,
        upstream_status: Some(status),
    })
}

fn write_request_bytes(worker: &mut TcpStream, bytes: &[u8]) -> io::Result<usize> {
    let mut written = 0;
    while written < bytes.len() {
        match worker.write(&bytes[written..]) {
            Ok(0) => return Ok(written),
            Ok(count) => written += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) if written > 0 => return Ok(written),
            Err(error) => return Err(error),
        }
    }
    Ok(written)
}

fn serialize_request(request: &QueuedRequest, worker_port: u16) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(256 + request.body.len());
    bytes.extend_from_slice(
        format!("{} {} HTTP/1.1\r\n", request.method, request.target).as_bytes(),
    );
    let mut has_host = false;
    let mut has_length = false;
    for header in request.headers.iter() {
        if is_hop_by_hop(&header.name) {
            continue;
        }
        if header.name.eq_ignore_ascii_case("Content-Length") {
            if has_length {
                continue;
            }
            has_length = true;
        }
        if header.name.eq_ignore_ascii_case("Host") {
            has_host = true;
        }
        bytes.extend_from_slice(header.name.as_bytes());
        bytes.extend_from_slice(b": ");
        bytes.extend_from_slice(header.value.as_bytes());
        bytes.extend_from_slice(b"\r\n");
    }
    if !has_host {
        bytes.extend_from_slice(format!("Host: 127.0.0.1:{worker_port}\r\n").as_bytes());
    }
    if !has_length {
        bytes.extend_from_slice(format!("Content-Length: {}\r\n", request.body.len()).as_bytes());
    }
    bytes.extend_from_slice(b"Connection: close\r\n\r\n");
    bytes.extend_from_slice(&request.body);
    bytes
}

fn is_hop_by_hop(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        "connection" | "keep-alive" | "proxy-connection" | "te" | "trailer" | "upgrade" | "expect"
    )
}

struct ResponseHead {
    status: u16,
    headers: Headers,
    leftover: Vec<u8>,
}

fn read_response_head(stream: &mut TcpStream) -> io::Result<Option<ResponseHead>> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        if raw.len() > MAX_HEADER_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "response header block is too large",
            ));
        }
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            if raw.is_empty() {
                return Ok(None);
            }
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "response ended before the header block",
            ));
        }
        raw.extend_from_slice(&buffer[..read]);
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or_default();
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid response status line")
        })?;
    let mut headers = Headers::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push(name.trim(), value.trim());
        }
    }
    Ok(Some(ResponseHead {
        status,
        headers,
        leftover: raw[header_end..].to_vec(),
    }))
}

enum Framing {
    Length(u64),
    Chunked,
    CloseDelimited,
}

fn response_framing(head: &ResponseHead) -> Framing {
    let chunked = head
        .headers
        .get_all("Transfer-Encoding")
        .iter()
        .flat_map(|value| value.split(','))
        .any(|token| token.trim().eq_ignore_ascii_case("chunked"));
    if chunked {
        return Framing::Chunked;
    }
    if let Some(length) = head
        .headers
        .get("Content-Length")
        .and_then(|value| value.trim().parse::<u64>().ok())
    {
        return Framing::Length(length);
    }
    Framing::CloseDelimited
}

fn write_client_response_head(
    client: &mut TcpStream,
    head: &ResponseHead,
    framing: &Framing,
) -> io::Result<()> {
    let mut headers = Vec::new();
    for header in head.headers.iter() {
        if is_hop_by_hop(&header.name) {
            continue;
        }
        if header.name.eq_ignore_ascii_case("Transfer-Encoding")
            && !matches!(framing, Framing::Chunked)
        {
            continue;
        }
        if header.name.eq_ignore_ascii_case("Content-Length")
            && !matches!(framing, Framing::Length(_))
        {
            continue;
        }
        headers.push((header.name.clone(), header.value.clone()));
    }
    if matches!(framing, Framing::CloseDelimited) {
        headers.push(("Connection".to_string(), "close".to_string()));
    }
    write_response_head(client, head.status, &headers)
}

fn relay_body(
    client: &mut TcpStream,
    worker: &mut TcpStream,
    framing: &Framing,
    leftover: Vec<u8>,
) -> io::Result<bool> {
    match framing {
        Framing::Length(length) => {
            copy_length(client, worker, leftover, *length)?;
            Ok(false)
        }
        Framing::Chunked => {
            copy_chunked(client, worker, leftover)?;
            Ok(false)
        }
        Framing::CloseDelimited => {
            copy_until_eof(client, worker, leftover)?;
            Ok(true)
        }
    }
}

fn copy_length(
    client: &mut TcpStream,
    worker: &mut TcpStream,
    pending: Vec<u8>,
    length: u64,
) -> io::Result<()> {
    let mut remaining = length;
    if !pending.is_empty() {
        let take = pending.len().min(remaining as usize);
        client.write_all(&pending[..take])?;
        remaining -= take as u64;
    }
    let mut buffer = [0u8; 8192];
    while remaining > 0 {
        let read = worker.read(&mut buffer)?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker ended before the declared response body",
            ));
        }
        let take = read.min(remaining as usize);
        client.write_all(&buffer[..take])?;
        remaining -= take as u64;
    }
    Ok(())
}

fn copy_chunked(
    client: &mut TcpStream,
    worker: &mut TcpStream,
    mut pending: Vec<u8>,
) -> io::Result<()> {
    enum ChunkState {
        Size,
        Data(u64),
        DataCrlf(usize),
        Trailers,
    }
    let mut state = ChunkState::Size;
    let mut buffer = [0u8; 8192];
    loop {
        match state {
            ChunkState::Size => {
                if let Some(position) = find_crlf(&pending) {
                    let size_text = String::from_utf8_lossy(&pending[..position]).to_string();
                    let size_text = size_text.split(';').next().unwrap_or("").trim();
                    let size = u64::from_str_radix(size_text, 16).map_err(|_| {
                        io::Error::new(io::ErrorKind::InvalidData, "invalid chunk size")
                    })?;
                    client.write_all(&pending[..position + 2])?;
                    pending.drain(..position + 2);
                    state = if size == 0 {
                        ChunkState::Trailers
                    } else {
                        ChunkState::Data(size)
                    };
                } else {
                    if pending.len() > MAX_HEADER_BYTES {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "chunk size line is too large",
                        ));
                    }
                    let read = worker.read(&mut buffer)?;
                    if read == 0 {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "worker ended inside a chunked body",
                        ));
                    }
                    pending.extend_from_slice(&buffer[..read]);
                }
            }
            ChunkState::Data(remaining) => {
                if pending.is_empty() {
                    let read = worker.read(&mut buffer)?;
                    if read == 0 {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "worker ended inside a chunked body",
                        ));
                    }
                    pending.extend_from_slice(&buffer[..read]);
                }
                let take = pending.len().min(remaining as usize);
                client.write_all(&pending[..take])?;
                pending.drain(..take);
                let remaining = remaining - take as u64;
                state = if remaining == 0 {
                    ChunkState::DataCrlf(0)
                } else {
                    ChunkState::Data(remaining)
                };
            }
            ChunkState::DataCrlf(read) => {
                if read < 2 {
                    if pending.is_empty() {
                        let count = worker.read(&mut buffer)?;
                        if count == 0 {
                            return Err(io::Error::new(
                                io::ErrorKind::UnexpectedEof,
                                "worker ended inside a chunked body",
                            ));
                        }
                        pending.extend_from_slice(&buffer[..count]);
                    }
                    let take = pending.len().min(2 - read);
                    client.write_all(&pending[..take])?;
                    pending.drain(..take);
                    state = ChunkState::DataCrlf(read + take);
                } else {
                    state = ChunkState::Size;
                }
            }
            ChunkState::Trailers => {
                if let Some(position) = find_crlf(&pending) {
                    let empty = position == 0;
                    client.write_all(&pending[..position + 2])?;
                    pending.drain(..position + 2);
                    if empty {
                        return Ok(());
                    }
                } else {
                    if pending.len() > MAX_HEADER_BYTES {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "chunk trailer line is too large",
                        ));
                    }
                    let read = worker.read(&mut buffer)?;
                    if read == 0 {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "worker ended inside chunked trailers",
                        ));
                    }
                    pending.extend_from_slice(&buffer[..read]);
                }
            }
        }
    }
}

fn copy_until_eof(
    client: &mut TcpStream,
    worker: &mut TcpStream,
    pending: Vec<u8>,
) -> io::Result<()> {
    if !pending.is_empty() {
        client.write_all(&pending)?;
    }
    let mut buffer = [0u8; 8192];
    loop {
        match worker.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(read) => client.write_all(&buffer[..read])?,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

fn find_crlf(bytes: &[u8]) -> Option<usize> {
    bytes.windows(2).position(|window| window == b"\r\n")
}

fn body_bytes(request: &Request) -> Option<&[u8]> {
    match &request.body {
        BodyRead::Complete(bytes) => Some(bytes),
        _ => None,
    }
}

fn write_error(
    client: &mut TcpStream,
    error: GatewayError,
    protocol: ErrorProtocol,
    request_id: &str,
) -> io::Result<bool> {
    let response = with_error_headers(error.response_for(protocol), request_id);
    let close = response.close;
    write_response(client, &response)?;
    Ok(close)
}

fn is_timeout_error(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_port_file(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "um-gateway-port-test-{}-{name}",
            std::process::id()
        ));
        let _ = std::fs::create_dir_all(&dir);
        dir.join("worker.port")
    }

    #[test]
    fn worker_port_file_rejects_a_port_only_record() {
        let path = temp_port_file("port-only");
        std::fs::write(&path, "12345\n").expect("write port file");
        assert_eq!(read_worker_port(&path), None);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn worker_port_file_accepts_the_current_pid() {
        let path = temp_port_file("current-pid");
        std::fs::write(&path, format!("12345\n{}\n", std::process::id())).expect("write port file");
        assert_eq!(read_worker_port(&path), Some(12345));
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(windows)]
    #[test]
    fn worker_port_file_rejects_a_foreign_pid() {
        let path = temp_port_file("foreign-pid");
        std::fs::write(&path, "12345\n0\n").expect("write port file");
        assert_eq!(read_worker_port(&path), None);
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(windows)]
    #[test]
    fn worker_port_file_rejects_a_live_process_with_a_different_image() {
        use std::process::{Command, Stdio};

        let mut child = Command::new("cmd")
            .args(["/C", "ping", "-n", "5", "127.0.0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn a different image");
        let path = temp_port_file("different-image");
        std::fs::write(&path, format!("12345\n{}\n", child.id())).expect("write port file");
        assert_eq!(read_worker_port(&path), None);
        let _ = child.kill();
        let _ = child.wait();
        let _ = std::fs::remove_file(&path);
    }
}
