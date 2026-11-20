//! Gateway HTTP router.
//!
//! The default candidate mode binds only a loopback ephemeral port
//! (`127.0.0.1:0`) and refuses a governed production port before the bind.
//! Production mode must be selected explicitly and binds only 8003/8002 on
//! loopback. Both modes share the same routes, authentication, ledger and error
//! contracts. The gateway never starts `chat2api.py`, the broker or the display
//! relay, and it never reads or writes production data on its own.

use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::{handle_chat, ChatResult};
use crate::config::{AuthMode, ConfigError, GatewayConfig, Role};
use crate::embeddings::handle_embeddings;
use crate::error::{ErrorProtocol, GatewayError};
use crate::event_log::EventLog;
use crate::gate::{
    constant_time_eq, ensure_control_token, Admission, InflightCtx, RefusalReason, ShareGate,
};
use crate::http::{
    random_hex, read_request_body, read_request_head, write_response, BodyRead, Headers, Request,
    RequestHead, Response, HARD_BODY_CAP,
};
use crate::ledger::Ledger;
use crate::login::{
    production_browser_factory, CredentialProbe, HttpCredentialProbe, LoginBrowserFactory,
    LoginService,
};
use crate::messages::handle_messages;
use crate::readonly::ProxyState;
use crate::reauth::{
    AuthWaitBudget, MissingLoginProvider, ReauthLimits, ReauthTrigger, ReauthenticationProvider,
    ReauthenticationService,
};
use crate::responses::handle_responses;
use crate::watch::WatchStatus;

const IDLE_CONNECTION_TIMEOUT: Duration = Duration::from_secs(30);
/// Python `AUTH_WATCH_INTERVAL_S`: the periodic credential probe interval.
const AUTH_WATCH_INTERVAL: Duration = Duration::from_secs(300);
/// Python `AUTH_WATCH_MAX_INTERVAL_S`: the rejected-credential backoff ceiling.
const AUTH_WATCH_MAX_INTERVAL: Duration = Duration::from_secs(1800);

/// A running gateway.
pub struct GatewayServer;

/// The state shared by every accepted connection.
pub struct AppState {
    pub config: GatewayConfig,
    pub proxy: Arc<ProxyState>,
    pub gate: Option<Arc<ShareGate>>,
    pub control_token: Option<String>,
    pub local_api_key: String,
    pub require_local_api_key: bool,
    pub ledger: Arc<Ledger>,
    pub event_log: Arc<EventLog>,
    pub reauth: Arc<ReauthenticationService>,
    pub login: Option<Arc<LoginService>>,
    /// The auth-watch state shared with `LoginService`; `/health` reads it even
    /// when the admin login surface is unassembled.
    pub watch: Arc<WatchStatus>,
    /// Set by `POST /admin/shutdown`; the process entry waits on it and then
    /// performs the graceful stop (listener, login browser, instance channel).
    pub shutdown_request: Arc<AtomicBool>,
}

/// A started gateway server.
pub struct ServerHandle {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    watcher: Option<JoinHandle<()>>,
    state: Arc<AppState>,
    #[cfg(unix)]
    instance: Option<crate::instance::InstanceChannel>,
}

impl GatewayServer {
    /// Start a gateway server; the socket is bound before the thread starts.
    pub fn start(config: GatewayConfig) -> Result<ServerHandle, ServerError> {
        Self::start_with_reauth(config, None)
    }

    /// Start a gateway server with an optional re-authentication provider.
    ///
    /// `None` assembles the production login executor: the real
    /// [`LoginService`] with the system browser factory and the HTTP
    /// credential probe. Tests use `Some` to script a session without opening
    /// a browser; the admin `/admin/auth/*` surface is then unassembled.
    pub fn start_with_reauth(
        config: GatewayConfig,
        provider: Option<Arc<dyn ReauthenticationProvider>>,
    ) -> Result<ServerHandle, ServerError> {
        Self::start_internal(config, provider, None)
    }

    /// Start a candidate server with an injected login executor seam.
    ///
    /// The injected factory and probe never open a real browser in tests; the
    /// real login service still owns the profile lock, token store, status,
    /// verify, login and cancel surfaces.
    pub fn start_with_login(
        config: GatewayConfig,
        factory: Arc<dyn LoginBrowserFactory>,
        probe: Arc<dyn CredentialProbe>,
    ) -> Result<ServerHandle, ServerError> {
        Self::start_internal(config, None, Some((factory, probe)))
    }

    fn start_internal(
        config: GatewayConfig,
        provider: Option<Arc<dyn ReauthenticationProvider>>,
        login_seam: Option<(Arc<dyn LoginBrowserFactory>, Arc<dyn CredentialProbe>)>,
    ) -> Result<ServerHandle, ServerError> {
        config.validate_bind_policy().map_err(ServerError::Config)?;
        let bearer = Arc::new(Mutex::new(config.upstream_token.clone()));
        let proxy = Arc::new(
            ProxyState::new_with_bearer(&config, Arc::clone(&bearer))
                .map_err(ServerError::Config)?,
        );
        proxy.probe_config();
        if let Some(document) = crate::login::load_saved_token(&config) {
            if !document.token.is_empty() {
                proxy.set_bearer(&document.token);
            }
            proxy.set_api_key(document.api_key.as_deref());
        }
        let watch = Arc::new(WatchStatus::new());
        let login = if provider.is_some() {
            None
        } else {
            let (factory, probe): (Arc<dyn LoginBrowserFactory>, Arc<dyn CredentialProbe>) =
                match login_seam {
                    Some(seam) => seam,
                    None => {
                        let probe = HttpCredentialProbe::from_config(&config)
                            .map_err(ServerError::Config)?;
                        (
                            production_browser_factory(&config),
                            Arc::new(probe) as Arc<dyn CredentialProbe>,
                        )
                    }
                };
            Some(Arc::new(LoginService::from_gateway_with_watch(
                &config,
                factory,
                probe,
                Arc::clone(&proxy) as Arc<dyn crate::login::CredentialSink>,
                Arc::clone(&watch),
            )))
        };
        let provider = provider
            .or_else(|| {
                login
                    .clone()
                    .map(|login| login as Arc<dyn ReauthenticationProvider>)
            })
            .unwrap_or_else(|| Arc::new(MissingLoginProvider) as Arc<dyn ReauthenticationProvider>);
        let mut limits = ReauthLimits::from_config(&config);
        if login.is_some() {
            // The login executor owns the rolling global start limit because
            // the explicit `/admin/auth/login` entry must be gated too.
            limits.max_attempts_per_window = usize::MAX;
        }
        let reauth = Arc::new(ReauthenticationService::new(provider, limits));
        let gate = match &config.share_config {
            Some(path) => Some(Arc::new(
                ShareGate::new(path, config.auth_mode).map_err(ServerError::Config)?,
            )),
            None => None,
        };
        // The broker's publish/drain/emergency verbs address this process over
        // the instance channel; without it every key change would fall back to an
        // emergency host stop.
        #[cfg(unix)]
        let instance = match (&gate, &config.instance_key_file) {
            (Some(gate), Some(key_file)) => {
                let secret =
                    crate::instance::read_instance_secret(key_file).map_err(ServerError::Io)?;
                let handler = Arc::new(crate::instance::InstanceHandler::new(
                    Arc::clone(gate),
                    secret,
                ));
                let prefix = config
                    .instance_pipe_prefix
                    .clone()
                    .unwrap_or_else(|| "/run/um-share/instance-".to_string());
                let path = std::path::PathBuf::from(format!("{prefix}{}", handler.instance_id()));
                Some(
                    crate::instance::InstanceChannel::start(
                        path,
                        handler,
                        config.registration_file.clone(),
                    )
                    .map_err(ServerError::Io)?,
                )
            }
            _ => None,
        };
        let control_token = config
            .control_token_file
            .as_ref()
            .and_then(|path| ensure_control_token(path));
        let local_api_key = config.local_api_key.clone();
        let require_local_api_key = !local_api_key.is_empty();
        let ledger = Arc::new(Ledger::new(config.usage_log.clone()));
        let event_log = Arc::new(EventLog::new(config.service_log.clone()));
        let state = Arc::new(AppState {
            config: config.clone(),
            proxy,
            gate,
            control_token,
            local_api_key,
            require_local_api_key,
            ledger,
            event_log,
            reauth,
            login,
            watch,
            shutdown_request: Arc::new(AtomicBool::new(false)),
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
        let watcher = spawn_auth_watch(Arc::clone(&state), Arc::clone(&shutdown));
        Ok(ServerHandle {
            addr,
            shutdown,
            handle: Some(handle),
            watcher: Some(watcher),
            state,
            #[cfg(unix)]
            instance,
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
            #[cfg(unix)]
            if let Some(instance) = &mut self.instance {
                instance.stop();
            }
            if let Some(login) = &self.state.login {
                login.shutdown();
            }
            self.shutdown.store(true, Ordering::SeqCst);
            let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
            let _ = handle.join();
        }
        if let Some(watcher) = self.watcher.take() {
            let _ = watcher.join();
        }
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// The Python `run_auth_watch_round` loop: every five minutes probe the
/// credential and start the ONE login round when it is no longer valid.
///
/// The first round waits one full base interval, like Python's
/// `begin_auth_watch()`: the startup probe already covers process start. A valid
/// round resets the interval to the base; any non-valid round (including
/// `unknown` and `browser_disabled`) doubles it up to 30 minutes. The interval
/// gating the next round is published to the shared `watch` view so
/// `next_round_due` follows the real backoff. Without this loop a token that
/// expires server-side leaves `/ready` at 503 until a client request happens to
/// trigger the round.
fn spawn_auth_watch(state: Arc<AppState>, shutdown: Arc<AtomicBool>) -> JoinHandle<()> {
    let watch = Arc::clone(&state.watch);
    thread::spawn(move || {
        watch.mark_started();
        let base_interval = state
            .config
            .auth_watch_interval
            .unwrap_or(AUTH_WATCH_INTERVAL);
        let mut interval = base_interval;
        let mut next_probe = Instant::now() + interval;
        state.proxy.record_next_check_after(interval);
        while !shutdown.load(Ordering::SeqCst) {
            if state.config.role != crate::config::Role::Local {
                next_probe = Instant::now() + interval;
            }
            while !shutdown.load(Ordering::SeqCst) {
                let remaining = next_probe.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                thread::sleep(remaining.min(Duration::from_secs(1)));
            }
            if shutdown.load(Ordering::SeqCst) {
                break;
            }
            let (_ready, snapshot) = state
                .proxy
                .readiness_snapshot_from(crate::upstream_fault::FaultSource::BackgroundProbe);
            let verdict = snapshot
                .get("authentication")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let reason = snapshot.get("reason").and_then(Value::as_str);
            let upstream_status = snapshot
                .get("upstream_status")
                .and_then(Value::as_u64)
                .map(|value| value as u16);
            if let Some(login) = &state.login {
                login.note_probe(verdict, reason, upstream_status);
            }
            let next_interval = if verdict == "valid" {
                base_interval
            } else {
                (interval * 2).min(AUTH_WATCH_MAX_INTERVAL)
            };
            watch.record_round(verdict, next_interval);
            state.proxy.record_next_check_after(next_interval);
            // 本机巡检可能先等待认证恢复，复查仍按已公布的时间执行。
            next_probe = Instant::now() + next_interval;
            interval = next_interval;
            if verdict == "valid" {
                continue;
            }
            if verdict != "rejected" && verdict != "needs_login" {
                // 补偿上游目录响应慢或临时不可达：稍后重新核验，不能重开登录。
                // `browser_disabled` 同样走退避，不能每轮都以基准间隔重试。
                continue;
            }
            if state.config.browser_disabled {
                continue;
            }
            let budget_limit = state
                .config
                .auth_wait_budget
                .unwrap_or_else(|| state.config.role.default_auth_wait_budget());
            let mut budget = AuthWaitBudget::new(budget_limit);
            let deadline = Instant::now() + state.config.login_timeout;
            let _ = state
                .reauth
                .trigger(ReauthTrigger::AuthWatch, &mut budget, deadline);
        }
        watch.mark_stopped();
    })
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

enum RouteOutcome {
    Response(Response),
    Written { close: bool },
}

fn handle_connection(state: Arc<AppState>, mut stream: TcpStream, peer_ip: IpAddr) {
    // The accept loop polls in non-blocking mode; Windows copies that mode into
    // accepted sockets, so restore blocking reads and let the read timeout cover
    // a client that connects before it sends its request.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IDLE_CONNECTION_TIMEOUT));
    loop {
        let head = match read_request_head(&mut stream, peer_ip) {
            Ok(Some(head)) => head,
            Ok(None) => return,
            Err(_) => {
                let request_id = random_hex(16);
                let response = with_error_headers(
                    GatewayError::invalid_request("Invalid request line or headers").response(),
                    &request_id,
                );
                let _ = write_response(&mut stream, &response);
                return;
            }
        };
        let path = head.path().to_string();
        // Method semantics come before authentication and before the body is
        // read: an unimplemented method on a known API path is a 405 with
        // `Allow`, not a 501/404 or a body-read timeout. In share mode the
        // non-whitelisted `/v1/*` paths keep their 404 `route_not_shared`
        // answer, so the route gate keeps precedence over the method check.
        let share_path_is_hidden = state.gate.is_some()
            && path.starts_with("/v1/")
            && !matches!(
                path.as_str(),
                "/v1/models" | "/v1/chat/completions" | "/v1/messages" | "/v1/responses"
            );
        if head.method != "OPTIONS" && !share_path_is_hidden {
            if let Some(allow) = allowed_methods(&path) {
                if !method_allowed(allow, &head.method) {
                    let mut response =
                        GatewayError::method_not_allowed().response_for(error_protocol(&path));
                    response = response.with_header("Allow", allow);
                    if let Some(origin) = cors_allow_origin(&state, &head.headers) {
                        response = response.with_header("Access-Control-Allow-Origin", origin);
                    }
                    let response = with_error_headers(response, &head.request_id);
                    let _ = write_response(&mut stream, &response);
                    return;
                }
            }
        }
        let request_id = head.request_id.clone();
        let max_body = body_read_cap(&state, &head);
        let _ = stream.set_read_timeout(Some(state.config.body_read_timeout));
        let body_result = read_request_body(&mut stream, head, max_body);
        let _ = stream.set_read_timeout(Some(IDLE_CONNECTION_TIMEOUT));
        let request = match body_result {
            Ok((request, _trailing)) => request,
            Err(error) if is_timeout_error(&error) => {
                let response = with_error_headers(
                    GatewayError::request_timeout().response_for(error_protocol(&path)),
                    &request_id,
                );
                let _ = write_response(&mut stream, &response);
                return;
            }
            Err(_) => {
                let response = with_error_headers(
                    GatewayError::invalid_request("Invalid request line or headers").response(),
                    &request_id,
                );
                let _ = write_response(&mut stream, &response);
                return;
            }
        };
        let (outcome, admission) = dispatch(&state, &request, &mut stream);
        let (close, write_ok) = match outcome {
            RouteOutcome::Response(response) => {
                let close = response.close;
                (close, write_response(&mut stream, &response).is_ok())
            }
            RouteOutcome::Written { close } => (close, true),
        };
        release_admission(&state, admission);
        if close || !write_ok {
            return;
        }
    }
}

/// The methods implemented for a known API path; `None` keeps the path's
/// existing 404 behavior (unknown paths are not 405s).
fn allowed_methods(path: &str) -> Option<&'static str> {
    match path {
        "/" | "/health" | "/ready" | "/v1/version" | "/v1/models" | "/api/models"
        | "/api/v1/models" => Some("GET"),
        "/v1/chat/completions"
        | "/api/chat/completions"
        | "/api/v1/chat/completions"
        | "/v1/messages"
        | "/api/v1/messages"
        | "/v1/responses"
        | "/api/v1/responses"
        | "/v1/embeddings"
        | "/api/v1/embeddings"
        | "/admin/share/revoke"
        | "/admin/auth/login"
        | "/admin/auth/cancel"
        | "/admin/shutdown" => Some("POST"),
        "/admin/auth/status" | "/admin/auth/verify" => Some("GET"),
        _ => None,
    }
}

fn method_allowed(allow: &str, method: &str) -> bool {
    allow
        .split(',')
        .map(str::trim)
        .any(|allowed| allowed == method)
}

fn is_timeout_error(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
    )
}

/// Add the error-response headers every self-produced error carries.
///
/// The request ID is generated once per request, so an in-band stream error can
/// repeat the same value inside its event body.
fn with_error_headers(mut response: Response, request_id: &str) -> Response {
    if response.status < 400 {
        return response;
    }
    if response.header("cache-control").is_none() {
        response = response.with_header("Cache-Control", "no-store");
    }
    if response.header("x-request-id").is_none() {
        response = response.with_header("X-Request-Id", request_id);
    }
    response
}

/// The pre-read body cap for one request.
///
/// The Python handler only checks `Content-Length` inside `_read_body`, so the
/// cap applies exactly to the routes that call it: the 8003 share routes and the
/// local `/api/*` passthrough use the share limit while a share gate is
/// assembled and `MAX_REQUEST_BYTES` otherwise; `/admin/share/revoke` uses the
/// same `_read_body`; every other route never reads a body and keeps the hard
/// transport cap.
fn body_read_cap(state: &AppState, head: &RequestHead) -> u64 {
    let path = head.path();
    let reads_body = head.method == "POST"
        && (path.starts_with("/v1/") || path.starts_with("/api/") || path == "/admin/share/revoke");
    if !reads_body {
        return HARD_BODY_CAP;
    }
    if state.gate.is_some() {
        state.config.share_max_request_bytes
    } else {
        state.config.max_request_bytes
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
    stream: &mut TcpStream,
) -> (RouteOutcome, Option<Admission>) {
    let (outcome, admission) = dispatch_inner(state, request, stream);
    let outcome = match outcome {
        RouteOutcome::Response(mut response) => {
            response = with_error_headers(response, &request.request_id);
            if response.header("access-control-allow-origin").is_none() {
                if let Some(origin) = cors_allow_origin(state, &request.headers) {
                    response = response.with_header("Access-Control-Allow-Origin", origin);
                }
            }
            RouteOutcome::Response(response)
        }
        written => written,
    };
    (outcome, admission)
}

fn dispatch_inner(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
) -> (RouteOutcome, Option<Admission>) {
    if request.method == "OPTIONS" {
        return (
            RouteOutcome::Response(options_response(state, &request.headers)),
            None,
        );
    }

    let path = request.path();
    let protocol = error_protocol(path);
    let share_mode = state.gate.is_some();
    let mut admission = None;
    if share_mode && path.starts_with("/v1/") {
        let allow_api_key = request.method == "GET" || path.starts_with("/v1/messages");
        match admit_share(state, request, stream, allow_api_key) {
            Ok(value) => admission = Some(value),
            Err(error) => return (RouteOutcome::Response(error.response_for(protocol)), None),
        }
    }

    if request.method == "POST" && is_chat_path(path) {
        if let Err(error) = local_key_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        if let Err(error) = origin_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        let result = handle_chat(state, request, stream, admission.take());
        return (
            match result {
                ChatResult::Response(response) => RouteOutcome::Response(response),
                ChatResult::Written { close, .. } => RouteOutcome::Written { close },
            },
            None,
        );
    }

    if request.method == "POST" && (path == "/v1/messages" || path == "/api/v1/messages") {
        if let Err(error) = local_key_allowed(state, request) {
            return (
                RouteOutcome::Response(error.response_for(protocol)),
                admission,
            );
        }
        if let Err(error) = origin_allowed(state, request) {
            return (
                RouteOutcome::Response(error.response_for(protocol)),
                admission,
            );
        }
        let result = handle_messages(state, request, stream, admission.take());
        return (
            match result {
                ChatResult::Response(response) => RouteOutcome::Response(response),
                ChatResult::Written { close, .. } => RouteOutcome::Written { close },
            },
            None,
        );
    }

    if request.method == "POST" && (path == "/v1/responses" || path == "/api/v1/responses") {
        if let Err(error) = local_key_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        if let Err(error) = origin_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        let result = handle_responses(state, request, stream, admission.take());
        return (
            match result {
                ChatResult::Response(response) => RouteOutcome::Response(response),
                ChatResult::Written { close, .. } => RouteOutcome::Written { close },
            },
            None,
        );
    }

    if request.method == "POST" && (path == "/v1/embeddings" || path == "/api/v1/embeddings") {
        if let Err(error) = local_key_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        if let Err(error) = origin_allowed(state, request) {
            return (RouteOutcome::Response(error.response()), admission);
        }
        let result = handle_embeddings(state, request, stream);
        return (
            match result {
                ChatResult::Response(response) => RouteOutcome::Response(response),
                ChatResult::Written { close, .. } => RouteOutcome::Written { close },
            },
            None,
        );
    }

    let response = match request.method.as_str() {
        "GET" => handle_get(state, request),
        "POST" => handle_post(state, request),
        _ => GatewayError::unknown_path(path).response(),
    };
    (RouteOutcome::Response(response), admission)
}

/// The R13 gate: only a verified `(key, model, protocol)` triple selected in the
/// trusted startup scope may use the aggregated upstream SSE transport.
pub fn nonstream_aggregate_allowed(
    state: &AppState,
    protocol: &str,
    model: &str,
    key_id: Option<&str>,
) -> bool {
    if !state.config.nonstream_upstream_aggregate {
        return false;
    }
    let Some(scope) = &state.config.nonstream_aggregate_scope else {
        return false;
    };
    let Some(key_id) = key_id.filter(|key_id| !key_id.is_empty()) else {
        return false;
    };
    !model.is_empty()
        && scope.key_ids.contains(key_id)
        && scope.models.contains(model)
        && scope.protocols.contains(protocol)
}

fn is_chat_path(path: &str) -> bool {
    matches!(
        path,
        "/v1/chat/completions" | "/api/chat/completions" | "/api/v1/chat/completions"
    )
}

/// `/v1/messages` and its local alias use the Anthropic error shape; every
/// other gateway route uses the OpenAI shape.
fn error_protocol(path: &str) -> ErrorProtocol {
    if path == "/v1/messages" || path == "/api/v1/messages" {
        ErrorProtocol::Anthropic
    } else {
        ErrorProtocol::OpenAi
    }
}

fn admit_share(
    state: &AppState,
    request: &Request,
    stream: &TcpStream,
    allow_api_key: bool,
) -> Result<Admission, GatewayError> {
    let path = request.path();
    if path.starts_with("/v1/models") {
        model_catalog_limit(request.raw_target()).map_err(|_| GatewayError::route_not_shared())?;
    } else if path.starts_with("/v1/messages") {
        messages_query(request.raw_target()).map_err(|_| GatewayError::route_not_shared())?;
    } else if !matches!(path, "/v1/chat/completions" | "/v1/responses") {
        return Err(GatewayError::route_not_shared());
    }
    let gate = state.gate.as_ref().expect("share mode gate");
    let (entry, config) = match gate.authenticate(&request.headers, allow_api_key) {
        Ok(Some(found)) => found,
        Ok(None) => return Err(GatewayError::invalid_api_key()),
        Err(error) => return Err(GatewayError::share_config_unavailable(error.to_string())),
    };
    let mut reason = RefusalReason::default();
    match gate.acquire(
        &entry.key_id,
        Some(config.config_generation),
        Some(entry.secret_version),
        Some(&mut reason),
    ) {
        Ok(true) => {}
        Ok(false) => {
            let mut error = GatewayError::sharing_limit_reached(
                reason.reason.as_deref(),
                reason.detail.as_deref(),
            );
            if let Some(seconds) = reason.retry_after_seconds {
                error = error.with_retry_after(seconds.to_string());
            }
            if let Some(should_retry) = reason.should_retry {
                error = error.with_should_retry(should_retry);
            }
            return Err(error);
        }
        Err(error) => return Err(GatewayError::share_config_unavailable(error.to_string())),
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

fn health_snapshot(state: &AppState) -> Value {
    let mut snapshot = state.proxy.health_snapshot();
    // `ProxyState::health_snapshot()` keeps the honest pre-watch defaults;
    // the server owns the real watch state, shared with `LoginService`.
    let watch = state.watch.snapshot();
    if let Some(object) = snapshot.as_object_mut() {
        object.insert("watch".to_string(), watch);
    }
    let Some(login) = &state.login else {
        return snapshot;
    };
    let status = login.status();
    let Some(object) = snapshot.as_object_mut() else {
        return snapshot;
    };
    let error = status.get("error").cloned().unwrap_or(Value::Null);
    let manual_error = error
        .as_str()
        .filter(|error| error.starts_with(crate::login::MANUAL_TAKEOVER_REQUIRED_PREFIX))
        .map(|error| Value::String(error.to_string()))
        .unwrap_or(Value::Null);
    let primary = status.get("primary").cloned().unwrap_or(Value::Null);
    object.insert(
        "authentication".to_string(),
        json!({
            "state": status.get("state").cloned().unwrap_or(Value::Null),
            "label": status.get("label").cloned().unwrap_or(Value::Null),
            "error": manual_error,
            "primary": health_primary(&primary),
        }),
    );
    let recovery = status.get("recovery").cloned().unwrap_or(Value::Null);
    object.insert(
        "recovery".to_string(),
        json!({
            "state": recovery.get("state").cloned().unwrap_or(Value::Null),
            "session_starts": recovery.get("session_starts").cloned().unwrap_or(Value::Null),
            "max_sessions": recovery.get("max_sessions").cloned().unwrap_or(Value::Null),
            "next_attempt_epoch": recovery
                .get("next_attempt_epoch")
                .cloned()
                .unwrap_or(Value::Null),
        }),
    );
    object.insert(
        "login_window_deadline_epoch".to_string(),
        status
            .get("login_window_deadline_epoch")
            .cloned()
            .unwrap_or(Value::Null),
    );
    object.insert(
        "login_window_s".to_string(),
        status.get("login_window_s").cloned().unwrap_or(Value::Null),
    );
    snapshot
}

/// `/health` has no control token: only a reason code or class name may pass.
fn health_primary(value: &Value) -> Value {
    let Some(value) = value.as_str() else {
        return Value::Null;
    };
    if !value.is_empty()
        && value.split(':').all(|part| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|character| character == '_' || character.is_alphanumeric())
        })
    {
        Value::String(value.to_string())
    } else {
        Value::Null
    }
}

fn handle_get(state: &AppState, request: &Request) -> Response {
    let path = request.path();
    match path {
        "/" | "/health" => Response::json(200, &health_snapshot(state)),
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
        "/admin/auth/status" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            match &state.login {
                Some(login) => Response::json(200, &login.status()),
                None => GatewayError::not_implemented("login_unassembled").response(),
            }
        }
        "/admin/auth/verify" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            Response::json(200, &state.proxy.verify_snapshot())
        }
        "/admin/auth/login" | "/admin/auth/cancel" | "/admin/shutdown" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            GatewayError::method_not_allowed().response()
        }
        path if path.starts_with("/api/") => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            if state.config.role == Role::Local {
                return match state.proxy.passthrough("GET", path, None) {
                    Ok(response) => response,
                    Err(error) => error.response(),
                };
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
            let Some(gate) = &state.gate else {
                return GatewayError::new(
                    503,
                    "Share revoke endpoint is not assembled in this role",
                    "share_revoke_unassembled",
                )
                .with_detail(json!({
                    "released": 0,
                    "reason": "share_backend_unassembled",
                }))
                .response();
            };
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            // The 8003 revoke route uses `_read_body`, not the 8002 console
            // boundary: no Content-Type requirement, duplicate sensitive
            // headers tolerated, share body limit and share error codes.
            let body = match read_json_body(
                request,
                BodyPolicy::Share {
                    max_request_bytes: state.config.share_max_request_bytes,
                },
            ) {
                Ok(body) => body,
                Err(error) => return error.response(),
            };
            let body = body.unwrap_or_default();
            let key_id = body.get("key_id").and_then(Value::as_str);
            let Some(key_id) = key_id.filter(|value| !value.is_empty()) else {
                return GatewayError::invalid_request("key_id is required").response();
            };
            Response::json(200, &json!({"released": gate.terminate_key(key_id)}))
        }
        "/admin/auth/login" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            let body = match read_json_body(request, body_policy_for(state, state.gate.is_some())) {
                Ok(body) => body.unwrap_or_default(),
                Err(error) => return error.response(),
            };
            let visible = match body.get("visible") {
                None => false,
                Some(Value::Bool(value)) => *value,
                Some(_) => {
                    return GatewayError::invalid_request("visible must be a boolean").response()
                }
            };
            match &state.login {
                Some(login) => Response::json(202, &login.start_explicit(visible)).closing(),
                None => GatewayError::not_implemented("login_unassembled").response(),
            }
        }
        "/admin/auth/cancel" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            match &state.login {
                Some(login) => Response::json(200, &login.cancel()).closing(),
                None => GatewayError::not_implemented("login_unassembled").response(),
            }
        }
        "/admin/auth/status" | "/admin/auth/verify" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            GatewayError::method_not_allowed().response()
        }
        "/admin/shutdown" => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            state.shutdown_request.store(true, Ordering::SeqCst);
            Response::json(202, &serde_json::json!({"status": "stopping"})).closing()
        }
        path if path.starts_with("/api/") => {
            if let Err(error) = control_allowed(state, request) {
                return error.response();
            }
            if state.config.role != Role::Local {
                return GatewayError::not_implemented("local_api_surface_s1a").response();
            }
            let body = match read_json_body(
                request,
                BodyPolicy::Share {
                    max_request_bytes: state.config.max_request_bytes,
                },
            ) {
                Ok(Some(body)) => serde_json::to_vec(&Value::Object(body)).unwrap_or_default(),
                Ok(None) => b"{}".to_vec(),
                Err(error) => return error.response(),
            };
            match state.proxy.passthrough("POST", path, Some(&body)) {
                Ok(response) => response,
                Err(error) => error.response(),
            }
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
    let mut supplied = if authorization.to_ascii_lowercase().starts_with("bearer ") {
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
    let supplied = request.headers.get("X-Admin-Control").unwrap_or_default();
    if expected.is_empty() || !constant_time_eq(supplied.as_bytes(), expected.as_bytes()) {
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
    matches!(
        hostname.to_ascii_lowercase().as_str(),
        "127.0.0.1" | "localhost" | "::1"
    )
}

/// A bounded query that does not match the SDK shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidQuery;

/// Accept only Harness's bounded catalog query.
pub fn model_catalog_limit(raw_target: &str) -> Result<Option<u32>, InvalidQuery> {
    if raw_target == "/v1/models" {
        return Ok(None);
    }
    let Some(query) = raw_target.strip_prefix("/v1/models?") else {
        return Err(InvalidQuery);
    };
    if query.is_empty() {
        return Err(InvalidQuery);
    }
    let mut limit = None;
    let mut seen = std::collections::BTreeSet::new();
    for part in query.split('&') {
        let (name, value) = part.split_once('=').ok_or(InvalidQuery)?;
        if name.is_empty() || !seen.insert(name) {
            return Err(InvalidQuery);
        }
        if name == "beta" && value == "true" {
            continue;
        }
        if name != "limit" {
            return Err(InvalidQuery);
        }
        if !value.is_ascii() || !value.chars().all(|character| character.is_ascii_digit()) {
            return Err(InvalidQuery);
        }
        if !(1..=4).contains(&value.len()) {
            return Err(InvalidQuery);
        }
        let parsed = value.parse::<u32>().map_err(|_| InvalidQuery)?;
        if !(1..=1000).contains(&parsed) || parsed.to_string() != value {
            return Err(InvalidQuery);
        }
        limit = Some(parsed);
    }
    Ok(limit)
}

/// Accept the SDK's beta query on Messages, never an arbitrary one.
pub fn messages_query(raw_target: &str) -> Result<(), InvalidQuery> {
    if raw_target == "/v1/messages" || raw_target == "/v1/messages?beta=true" {
        Ok(())
    } else {
        Err(InvalidQuery)
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
    if !body
        .get("model")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty())
    {
        return false;
    }
    if !body
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|value| !value.is_empty())
    {
        return false;
    }
    if !matches!(body.get("stream"), None | Some(Value::Bool(_))) {
        return false;
    }
    !body.contains_key("store") || body.get("store") == Some(&Value::Bool(false))
}

/// Test seam: the body policy used for a route group.
pub fn body_policy_for(state: &AppState, share: bool) -> BodyPolicy {
    if share {
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        }
    } else {
        BodyPolicy::Share {
            max_request_bytes: state.config.max_request_bytes,
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
