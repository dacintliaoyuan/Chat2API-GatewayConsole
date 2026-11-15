//! S7: the private noVNC display surface.
//!
//! This is `console/display_proxy.py`: the launcher page, its module assets,
//! the read-only noVNC tree, one-time display grants and the WebSocket-to-RFB
//! relay. The candidate assembles only a loopback listener; the fake x11vnc
//! used by tests is never the real display on 8097.

use std::io::{Read, Write};
use std::net::{IpAddr, Shutdown, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::http::{write_response_head, BodyRead, Headers, Request, Response};

use crate::display_grant::DisplayGrants;
use crate::session::SessionState;
use crate::share_auth::control_token_ok;

pub const DISPLAY_PREFIX: &str = "/display";
pub const WEBSOCKET_PATH: &str = "/display/websockify";
pub const GRANT_PATH: &str = "/display/grant";
pub const SESSION_PATH: &str = "/display/session";
pub const DISPLAY_SESSION_COOKIE: &str = "um_display_session";
pub const LAUNCHER_SCRIPT_PATH: &str = "launcher.js";
pub const LAUNCHER_SCRIPT_NAME: &str = "novnc-launcher.js";
pub const LAUNCHER_STATE_SCRIPT_PATH: &str = "launcher-state.js";
pub const LAUNCHER_STATE_SCRIPT_NAME: &str = "novnc-launcher-state.mjs";

/// `console/display_proxy.py::DEFAULT_VNC_PORT`.
pub const DEFAULT_VNC_PORT: u16 = 8097;

pub const MAX_GRANT_BODY: u64 = 4096;
pub const MAX_GRANT_TOKEN: usize = 256;

/// `console/display_proxy.py::WS_GUID` (RFC 6455 handshake only).
pub const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
pub const MAX_FRAME_PAYLOAD: usize = 8 * 1024 * 1024;
pub const UPSTREAM_READ_SIZE: usize = 64 * 1024;
pub const PING_INTERVAL: Duration = Duration::from_secs(30);
pub const MAX_CLOSE_REASON: usize = 123;
pub const CLIENT_FRAME_TIMEOUT: Duration = Duration::from_secs(2);
pub const SEND_FRAME_TIMEOUT: Duration = Duration::from_secs(2);
pub const CLOSE_FRAME_TIMEOUT: Duration = Duration::from_millis(250);
/// How long a relay waits for its worker threads after the session ended.
pub const RELAY_POLL_INTERVAL: Duration = Duration::from_millis(100);

pub const STATIC_TYPES: [(&str, &str); 11] = [
    (".js", "text/javascript; charset=utf-8"),
    (".mjs", "text/javascript; charset=utf-8"),
    (".css", "text/css; charset=utf-8"),
    (".png", "image/png"),
    (".svg", "image/svg+xml"),
    (".gif", "image/gif"),
    (".ico", "image/x-icon"),
    (".woff", "font/woff"),
    (".woff2", "font/woff2"),
    (".ttf", "font/ttf"),
    (".wasm", "application/wasm"),
];

pub const SECURITY_HEADERS: [(&str, &str); 7] = [
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    (
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    ),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
         img-src 'self' data:; connect-src 'self'; font-src 'self'; \
         worker-src 'self' blob:; base-uri 'none'; form-action 'none'; \
         frame-ancestors 'none'",
    ),
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DisplayConfig {
    pub enabled: bool,
    pub vnc_host: String,
    pub vnc_port: u16,
    pub novnc_root: PathBuf,
    pub launcher_file: PathBuf,
    pub max_connections: usize,
    pub idle_timeout: Duration,
    pub max_session_seconds: Duration,
    pub handshake_timeout: Duration,
    pub error: String,
    pub vnc_auth_socket: String,
    pub vnc_auth_file: String,
    pub relay_join_timeout: Duration,
}

impl Default for DisplayConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            vnc_host: "127.0.0.1".to_string(),
            vnc_port: DEFAULT_VNC_PORT,
            novnc_root: PathBuf::from("/usr/share/novnc"),
            launcher_file: PathBuf::new(),
            max_connections: 2,
            idle_timeout: Duration::from_secs(900),
            max_session_seconds: Duration::from_secs(14400),
            handshake_timeout: Duration::from_secs(10),
            error: String::new(),
            vnc_auth_socket: String::new(),
            vnc_auth_file: String::new(),
            relay_join_timeout: Duration::from_secs(5),
        }
    }
}

impl DisplayConfig {
    /// `display_proxy.py::load_config`: invalid values fail closed.
    pub fn load<F: Fn(&str) -> Option<String>>(env: F) -> Self {
        if !enabled(&env_value(&env, "CONSOLE_DISPLAY_ENABLED", "0")) {
            return Self::default();
        }
        let invalid = || Self {
            enabled: false,
            error: "display_config_invalid".to_string(),
            ..Self::default()
        };
        let vnc_host = env_value(&env, "CONSOLE_DISPLAY_VNC_HOST", "127.0.0.1")
            .trim()
            .to_string();
        let address = match vnc_host.parse::<IpAddr>() {
            Ok(address) if address.is_loopback() => address,
            _ => return invalid(),
        };
        let vnc_port = match parse_integer(
            &env_value(
                &env,
                "CONSOLE_DISPLAY_VNC_PORT",
                &DEFAULT_VNC_PORT.to_string(),
            ),
            1,
            65535,
        ) {
            Some(value) => value as u16,
            None => return invalid(),
        };
        let novnc_root = env_value(&env, "CONSOLE_DISPLAY_NOVNC_ROOT", "/usr/share/novnc")
            .trim()
            .to_string();
        if novnc_root.is_empty() || !absolute_path(&novnc_root) {
            return invalid();
        }
        let launcher_file = env_value(
            &env,
            "CONSOLE_DISPLAY_LAUNCHER_FILE",
            &default_launcher().to_string_lossy(),
        )
        .trim()
        .to_string();
        if launcher_file.is_empty() || !absolute_path(&launcher_file) {
            return invalid();
        }
        let max_connections = match parse_integer(
            &env_value(&env, "CONSOLE_DISPLAY_MAX_CONNECTIONS", "2"),
            1,
            4,
        ) {
            Some(value) => value as usize,
            None => return invalid(),
        };
        let idle_timeout = match parse_number(
            &env_value(&env, "CONSOLE_DISPLAY_IDLE_TIMEOUT", "900"),
            30.0,
            7200.0,
        ) {
            Some(value) => value,
            None => return invalid(),
        };
        let max_session_seconds = match parse_number(
            &env_value(&env, "CONSOLE_DISPLAY_MAX_SESSION", "14400"),
            60.0,
            14400.0,
        ) {
            Some(value) => value,
            None => return invalid(),
        };
        let handshake_timeout = match parse_number(
            &env_value(&env, "CONSOLE_DISPLAY_HANDSHAKE_TIMEOUT", "10"),
            1.0,
            30.0,
        ) {
            Some(value) => value,
            None => return invalid(),
        };
        let relay_join_timeout = match parse_number(
            &env_value(&env, "CONSOLE_DISPLAY_SHUTDOWN_GRACE", "5"),
            1.0,
            30.0,
        ) {
            Some(value) => value,
            None => return invalid(),
        };
        let vnc_auth_file = env_value(&env, "CONSOLE_DISPLAY_VNC_AUTH_FILE", "")
            .trim()
            .to_string();
        let vnc_auth_socket = env_value(
            &env,
            "CONSOLE_DISPLAY_VNC_AUTH_SOCKET",
            if vnc_auth_file.is_empty() {
                "/run/um-share/vnc-auth.sock"
            } else {
                ""
            },
        )
        .trim()
        .to_string();
        if !vnc_auth_socket.is_empty() && !absolute_path(&vnc_auth_socket) {
            return invalid();
        }
        if !vnc_auth_file.is_empty() && !absolute_path(&vnc_auth_file) {
            return invalid();
        }
        if !vnc_auth_socket.is_empty() && !vnc_auth_file.is_empty() {
            return invalid();
        }
        if !Path::new(&novnc_root).is_dir() {
            return Self {
                enabled: false,
                error: "display_novnc_missing".to_string(),
                ..Self::default()
            };
        }
        if !Path::new(&launcher_file).is_file() {
            return Self {
                enabled: false,
                error: "display_launcher_missing".to_string(),
                ..Self::default()
            };
        }
        if !vnc_auth_file.is_empty() && !Path::new(&vnc_auth_file).is_file() {
            return Self {
                enabled: false,
                error: "display_vnc_auth_missing".to_string(),
                ..Self::default()
            };
        }
        Self {
            enabled: true,
            vnc_host: address.to_string(),
            vnc_port,
            novnc_root: PathBuf::from(novnc_root),
            launcher_file: PathBuf::from(launcher_file),
            max_connections,
            idle_timeout: Duration::from_secs_f64(idle_timeout),
            max_session_seconds: Duration::from_secs_f64(max_session_seconds),
            handshake_timeout: Duration::from_secs_f64(handshake_timeout),
            error: String::new(),
            vnc_auth_socket,
            vnc_auth_file,
            relay_join_timeout: Duration::from_secs_f64(relay_join_timeout),
        }
    }

    pub fn from_env() -> Self {
        Self::load(|name| std::env::var(name).ok())
    }

    /// `_console_display_availability`: an enabled relay is available.
    pub fn availability(&self) -> Value {
        if self.enabled {
            json!({"available": true, "reason": Value::Null})
        } else {
            json!({
                "available": false,
                "reason": if self.error.is_empty() {
                    "display_disabled"
                } else {
                    self.error.as_str()
                },
            })
        }
    }
}

fn env_value<F: Fn(&str) -> Option<String>>(env: &F, name: &str, default: &str) -> String {
    env(name).unwrap_or_else(|| default.to_string())
}

fn enabled(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn parse_integer(value: &str, minimum: i64, maximum: i64) -> Option<i64> {
    let text = value.trim();
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let number = text.parse::<i64>().ok()?;
    (minimum..=maximum).contains(&number).then_some(number)
}

fn parse_number(value: &str, minimum: f64, maximum: f64) -> Option<f64> {
    let number = value.trim().parse::<f64>().ok()?;
    (minimum..=maximum).contains(&number).then_some(number)
}

/// `display_proxy.py::_absolute_path`: POSIX absolute on every host, or a
/// native absolute path.
fn absolute_path(value: &str) -> bool {
    value.starts_with('/') || Path::new(value).is_absolute()
}

/// `display_proxy.py::_default_launcher`: the repository launcher next to the
/// source tree. The deployment sets `CONSOLE_DISPLAY_LAUNCHER_FILE` explicitly.
fn default_launcher() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../um-cloud-deploy/display/novnc-launcher.html")
}

/// VNC challenge-response provider; the password never leaves the provider.
pub trait VncAuthProvider: Send + Sync {
    fn response(&self, challenge: &[u8; 16]) -> Result<[u8; 16], VncAuthError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VncAuthError {
    pub code: &'static str,
}

impl VncAuthError {
    pub fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl std::fmt::Display for VncAuthError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for VncAuthError {}

/// `display_proxy.py::ConnectionLimiter`: release every slot exactly once.
#[derive(Debug)]
pub struct ConnectionLimiter {
    limit: usize,
    active: AtomicUsize,
}

impl ConnectionLimiter {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            active: AtomicUsize::new(0),
        }
    }

    pub fn active(&self) -> usize {
        self.active.load(Ordering::SeqCst)
    }

    pub fn acquire(&self) -> bool {
        let mut current = self.active.load(Ordering::SeqCst);
        loop {
            if current >= self.limit {
                return false;
            }
            match self.active.compare_exchange(
                current,
                current + 1,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => return true,
                Err(observed) => current = observed,
            }
        }
    }

    pub fn release(&self) {
        let mut current = self.active.load(Ordering::SeqCst);
        while current > 0 {
            match self.active.compare_exchange(
                current,
                current - 1,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => return,
                Err(observed) => current = observed,
            }
        }
    }
}

pub struct DisplayRelay {
    config: DisplayConfig,
    limiter: ConnectionLimiter,
    auth_provider: Option<Arc<dyn VncAuthProvider>>,
}

impl DisplayRelay {
    pub fn new(config: DisplayConfig) -> Self {
        let auth_provider = build_auth_provider(&config);
        Self {
            limiter: ConnectionLimiter::new(config.max_connections),
            auth_provider,
            config,
        }
    }

    /// Test seam: run one challenge through the configured auth provider.
    pub fn auth_response_for_tests(&self, challenge: &[u8; 16]) -> Result<[u8; 16], VncAuthError> {
        self.auth_provider
            .as_ref()
            .ok_or_else(|| VncAuthError::new("display_vnc_auth_unconfigured"))?
            .response(challenge)
    }

    pub fn with_auth_provider(
        config: DisplayConfig,
        auth_provider: Option<Arc<dyn VncAuthProvider>>,
    ) -> Self {
        Self {
            limiter: ConnectionLimiter::new(config.max_connections),
            auth_provider,
            config,
        }
    }

    pub fn config(&self) -> &DisplayConfig {
        &self.config
    }

    pub fn active_connections(&self) -> usize {
        self.limiter.active()
    }

    pub fn handle_get(
        &self,
        request: &Request,
        console_port: u16,
        grants: &DisplayGrants,
        session: &SessionState,
        control_token_file: Option<&Path>,
    ) -> Option<Response> {
        let (path, query, fragment) = split_target(request.raw_target());
        if path != DISPLAY_PREFIX && !path.starts_with("/display/") {
            return None;
        }
        if fragment {
            return Some(display_json(400, "display query not allowed"));
        }
        if path == WEBSOCKET_PATH {
            if let Some(query) = query.filter(|query| !query.is_empty()) {
                let pairs = query_pairs(query);
                if pairs.len() != 1 || pairs[0].0 != "operator" {
                    return Some(display_json(400, "display query not allowed"));
                }
                if parse_operator_generation(&pairs[0].1).is_none() {
                    return Some(display_json(400, "display query not allowed"));
                }
            }
        } else if query.is_some_and(|query| !query.is_empty()) {
            return Some(display_json(400, "display query not allowed"));
        }
        if !self.config.enabled {
            return Some(display_json(503, self.unavailable_reason()));
        }
        if path == GRANT_PATH {
            // The grant page is static code, not a secret: it may load without
            // a session so its module script can exchange a grant fragment.
            if self
                .request_origin(
                    request,
                    console_port,
                    false,
                    false,
                    grants,
                    session,
                    control_token_file,
                )
                .is_none()
            {
                return Some(display_json(403, "display access denied"));
            }
            return Some(self.serve_static("/display/"));
        }
        if path == SESSION_PATH {
            if self
                .request_origin(
                    request,
                    console_port,
                    false,
                    false,
                    grants,
                    session,
                    control_token_file,
                )
                .is_none()
            {
                return Some(display_json(403, "display access denied"));
            }
            return Some(self.serve_session(
                request,
                console_port,
                grants,
                session,
                control_token_file,
            ));
        }
        let websocket = path == WEBSOCKET_PATH;
        let require_authorization = websocket || path == DISPLAY_PREFIX || path == "/display/";
        if self
            .request_origin(
                request,
                console_port,
                websocket,
                require_authorization,
                grants,
                session,
                control_token_file,
            )
            .is_none()
        {
            return Some(display_json(403, "display access denied"));
        }
        Some(self.serve_static(path))
    }

    pub fn handle_post(
        &self,
        request: &Request,
        console_port: u16,
        grants: &DisplayGrants,
        session: &SessionState,
        control_token_file: Option<&Path>,
    ) -> Option<Response> {
        let (path, query, fragment) = split_target(request.raw_target());
        if path != GRANT_PATH {
            if path == DISPLAY_PREFIX || path.starts_with("/display/") {
                return Some(display_json(404, "unknown display path"));
            }
            return None;
        }
        if query.is_some() || fragment {
            return Some(display_json(400, "display query not allowed"));
        }
        if !self.config.enabled {
            return Some(display_json(503, self.unavailable_reason()));
        }
        let Some(origin) = self.request_origin(
            request,
            console_port,
            false,
            false,
            grants,
            session,
            control_token_file,
        ) else {
            return Some(display_json(403, "display access denied"));
        };
        Some(self.serve_grant_exchange(request, origin, grants))
    }

    /// `display_proxy.py::DisplayProxy.handle_get` for `/display/websockify`.
    ///
    /// The raw TCP stream is taken over here because a successful upgrade
    /// never produces a normal `Response`.
    #[allow(clippy::too_many_arguments)]
    pub fn handle_websocket(
        &self,
        request: &Request,
        initial: Vec<u8>,
        stream: &mut TcpStream,
        console_port: u16,
        grants: &DisplayGrants,
        session: &SessionState,
        control_token_file: Option<&Path>,
    ) {
        let (path, query, fragment) = split_target(request.raw_target());
        if path != WEBSOCKET_PATH {
            return;
        }
        if fragment {
            let _ = write_display_json(stream, 400, "display query not allowed");
            return;
        }
        let mut operator = None;
        if let Some(query) = query.filter(|query| !query.is_empty()) {
            let pairs = query_pairs(query);
            if pairs.len() != 1 || pairs[0].0 != "operator" {
                let _ = write_display_json(stream, 400, "display query not allowed");
                return;
            }
            operator = parse_operator_generation(&pairs[0].1);
            if operator.is_none() {
                let _ = write_display_json(stream, 400, "display query not allowed");
                return;
            }
        }
        if !self.config.enabled {
            let _ = write_display_json(stream, 503, self.unavailable_reason());
            return;
        }
        let Some(origin) = self.request_origin(
            request,
            console_port,
            true,
            true,
            grants,
            session,
            control_token_file,
        ) else {
            let _ = write_display_json(stream, 403, "display access denied");
            return;
        };
        let (status, error, protocol) = validate_websocket_handshake(&request.headers);
        if status != 0 {
            let _ = write_display_json(stream, status, error);
            return;
        }
        // Only a live display session with the current operator generation may
        // open the relay; a missing or stale generation is refused before the
        // upstream is contacted.
        let cookie = header(&request.headers, "Cookie");
        let Some(session_token) =
            cookie.and_then(|header| cookie_value(header, DISPLAY_SESSION_COOKIE))
        else {
            let _ = write_display_json(stream, 403, "display access denied");
            return;
        };
        if grants.validate_session(&session_token, &origin).is_none() {
            let _ = write_display_json(stream, 403, "display access denied");
            return;
        }
        let Some(operator) = operator else {
            let _ = write_display_json(stream, 409, "display_operator_stale");
            return;
        };
        if !grants.operator_matches(Some(&session_token), operator) {
            let _ = write_display_json(stream, 409, "display_operator_stale");
            return;
        }
        if !self.limiter.acquire() {
            let _ = write_display_json(stream, 503, "display busy");
            return;
        }
        let _guard = ConnectionGuard {
            limiter: &self.limiter,
        };
        let Some(upstream) = connect_upstream(&self.config) else {
            let _ = write_display_json(stream, 502, "display upstream unavailable");
            return;
        };
        let greeting = match &self.auth_provider {
            Some(provider) => match perform_vnc_auth(
                upstream.try_clone().ok(),
                provider.as_ref(),
                self.config.handshake_timeout,
            ) {
                Ok(frames) => frames,
                Err(error) => {
                    let _ = write_display_json(stream, 502, error.code);
                    return;
                }
            },
            None => Vec::new(),
        };
        let mut headers = vec![
            ("Upgrade".to_string(), "websocket".to_string()),
            ("Connection".to_string(), "Upgrade".to_string()),
            (
                "Sec-WebSocket-Accept".to_string(),
                accept_key(header(&request.headers, "Sec-WebSocket-Key").unwrap_or_default()),
            ),
            ("Cache-Control".to_string(), "no-store".to_string()),
        ];
        if let Some(protocol) = protocol {
            headers.push(("Sec-WebSocket-Protocol".to_string(), protocol.to_string()));
        }
        if write_response_head(stream, 101, &headers).is_err() {
            return;
        }
        for payload in &greeting {
            if send_frame(stream, 0x02, payload, Some(SEND_FRAME_TIMEOUT)).is_err() {
                return;
            }
        }
        let input_allowed = |grants: &DisplayGrants, token: &str, generation: u64| {
            grants.operator_matches(Some(token), generation)
        };
        run_relay(
            stream,
            initial,
            upstream,
            RelayOptions {
                idle_timeout: self.config.idle_timeout,
                max_session_seconds: self.config.max_session_seconds,
                discard: if greeting.is_empty() { 0 } else { 14 },
                input_allowed: &|| input_allowed(grants, &session_token, operator),
            },
        );
        let _ = grants.release_operator(Some(&session_token), operator);
    }

    fn unavailable_reason(&self) -> &str {
        if self.config.error.is_empty() {
            "display unavailable"
        } else {
            self.config.error.as_str()
        }
    }

    fn serve_session(
        &self,
        request: &Request,
        console_port: u16,
        grants: &DisplayGrants,
        session: &SessionState,
        control_token_file: Option<&Path>,
    ) -> Response {
        let Some(origin) = origin_checked(request, console_port, false) else {
            return display_json(403, "display access denied");
        };
        let cookie = header(&request.headers, "Cookie");
        let display = display_session_valid(grants, cookie, &origin);
        let authorized = display
            || control_token_ok(
                control_token_file,
                header(&request.headers, "X-Admin-Control"),
            )
            || session.valid(cookie);
        let mut expires_at = Value::Null;
        if authorized {
            if let Some(session) = display_session(grants, cookie, &origin) {
                expires_at = json!(session.expires_at);
            }
        }
        display_json_body(json!({"authorized": authorized, "expires_at": expires_at}))
    }

    fn serve_grant_exchange(
        &self,
        request: &Request,
        origin: String,
        grants: &DisplayGrants,
    ) -> Response {
        let lengths = request.headers.get_all("Content-Length");
        if lengths.len() != 1 {
            return display_empty(400, &[]);
        }
        let Ok(length) = lengths[0].trim().parse::<i64>() else {
            return display_empty(400, &[]);
        };
        if length <= 0 {
            return display_empty(400, &[]);
        }
        if length as u64 > MAX_GRANT_BODY {
            return display_empty(413, &[]);
        }
        let body = match &request.body {
            BodyRead::Complete(bytes) if bytes.len() as i64 == length => bytes,
            _ => return display_empty(400, &[]),
        };
        let content_type = request.headers.get("Content-Type").unwrap_or_default();
        if !content_type
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .eq_ignore_ascii_case("application/json")
        {
            return display_empty(415, &[]);
        }
        let document: Value = match serde_json::from_slice(body) {
            Ok(value) => value,
            Err(_) => return display_empty(400, &[]),
        };
        let token = document.get("grant").and_then(Value::as_str);
        let Some(token) = token else {
            return display_empty(400, &[]);
        };
        let characters = token.chars().count();
        if !(16..=MAX_GRANT_TOKEN).contains(&characters)
            || token
                .chars()
                .any(|character| matches!(character, '\r' | '\n' | '\0'))
        {
            return display_empty(400, &[]);
        }
        let Some(session) = grants.redeem(token, &origin) else {
            return display_empty(403, &[]);
        };
        let now = epoch_seconds();
        let max_age = session.expires_at.saturating_sub(now).max(1);
        let mut cookie = format!(
            "{DISPLAY_SESSION_COOKIE}={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}",
            session.token
        );
        if origin.starts_with("https://") {
            cookie.push_str("; Secure");
        }
        display_empty(204, &[("Set-Cookie", &cookie)])
    }

    fn serve_static(&self, path: &str) -> Response {
        if path == DISPLAY_PREFIX {
            let mut response = Response::new(308, Vec::new()).with_header("Location", "/display/");
            for (name, value) in SECURITY_HEADERS {
                response = response.with_header(name, value);
            }
            response.close = true;
            return response;
        }
        let relative = &path[DISPLAY_PREFIX.len() + 1..];
        let launcher_directory = self
            .config
            .launcher_file
            .parent()
            .unwrap_or_else(|| Path::new(""));
        let (target, content_type) = if relative.is_empty() {
            (
                self.config.launcher_file.clone(),
                "text/html; charset=utf-8".to_string(),
            )
        } else if relative == LAUNCHER_SCRIPT_PATH {
            (
                launcher_directory.join(LAUNCHER_SCRIPT_NAME),
                "text/javascript; charset=utf-8".to_string(),
            )
        } else if relative == LAUNCHER_STATE_SCRIPT_PATH {
            (
                launcher_directory.join(LAUNCHER_STATE_SCRIPT_NAME),
                "text/javascript; charset=utf-8".to_string(),
            )
        } else if safe_asset(relative) {
            let Ok(root) = self.config.novnc_root.canonicalize() else {
                return display_json(404, "unknown display asset");
            };
            let Ok(target) = root.join(relative).canonicalize() else {
                return display_json(404, "unknown display asset");
            };
            if !target.starts_with(&root) {
                return display_json(404, "unknown display asset");
            }
            (
                target,
                static_type(relative)
                    .unwrap_or("application/octet-stream")
                    .to_string(),
            )
        } else {
            return display_json(404, "unknown display asset");
        };
        let Ok(body) = std::fs::read(&target) else {
            return display_json(404, "unknown display asset");
        };
        let mut response = Response::new(200, body).with_header("Content-Type", content_type);
        for (name, value) in SECURITY_HEADERS {
            response = response.with_header(name, value);
        }
        response
    }

    #[allow(clippy::too_many_arguments)]
    fn request_origin(
        &self,
        request: &Request,
        console_port: u16,
        websocket: bool,
        require_authorization: bool,
        grants: &DisplayGrants,
        session: &SessionState,
        control_token_file: Option<&Path>,
    ) -> Option<String> {
        let expected = origin_checked(request, console_port, websocket)?;
        if !require_authorization {
            return Some(expected);
        }
        let cookie = header(&request.headers, "Cookie");
        if display_session_valid(grants, cookie, &expected) {
            return Some(expected);
        }
        if control_token_ok(
            control_token_file,
            header(&request.headers, "X-Admin-Control"),
        ) {
            return Some(expected);
        }
        if session.valid(cookie) {
            return Some(expected);
        }
        None
    }
}

struct ConnectionGuard<'a> {
    limiter: &'a ConnectionLimiter,
}

impl Drop for ConnectionGuard<'_> {
    fn drop(&mut self) {
        self.limiter.release();
    }
}

fn connect_upstream(config: &DisplayConfig) -> Option<TcpStream> {
    let addresses = (config.vnc_host.as_str(), config.vnc_port)
        .to_socket_addrs()
        .ok()?;
    for address in addresses {
        if let Ok(stream) = TcpStream::connect_timeout(&address, config.handshake_timeout) {
            let _ = stream.set_nodelay(true);
            return Some(stream);
        }
    }
    None
}

fn write_display_json(stream: &mut TcpStream, status: u16, error: &str) -> std::io::Result<()> {
    let body = serde_json::to_vec(&json!({"error": error})).unwrap_or_default();
    let mut headers = vec![
        (
            "Content-Type".to_string(),
            "application/json; charset=utf-8".to_string(),
        ),
        ("Content-Length".to_string(), body.len().to_string()),
    ];
    for (name, value) in SECURITY_HEADERS {
        headers.push((name.to_string(), value.to_string()));
    }
    headers.push(("Connection".to_string(), "close".to_string()));
    write_response_head(stream, status, &headers)?;
    stream.write_all(&body)?;
    stream.flush()
}

/// `display_proxy.py::validate_websocket_handshake`: `(0, "", protocol)`
/// means a valid upgrade; any other status refuses before the relay starts.
fn validate_websocket_handshake(headers: &Headers) -> (u16, &'static str, Option<&'static str>) {
    let upgrade = header(headers, "Upgrade");
    if upgrade.map_or(true, |upgrade| {
        !upgrade.trim().eq_ignore_ascii_case("websocket")
    }) {
        return (400, "websocket upgrade required", None);
    }
    let connection = header(headers, "Connection").unwrap_or_default();
    if !connection
        .split(',')
        .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))
    {
        return (400, "websocket upgrade required", None);
    }
    if header(headers, "Sec-WebSocket-Version") != Some("13") {
        return (426, "unsupported websocket version", None);
    }
    let Some(key) = header(headers, "Sec-WebSocket-Key") else {
        return (400, "websocket key required", None);
    };
    let Some(decoded) = base64_decode(key) else {
        return (400, "invalid websocket key", None);
    };
    if decoded.len() != 16 {
        return (400, "invalid websocket key", None);
    }
    let mut protocols = Vec::new();
    for row in headers.get_all("Sec-WebSocket-Protocol") {
        protocols.extend(
            row.split(',')
                .map(str::trim)
                .filter(|protocol| !protocol.is_empty()),
        );
    }
    if !protocols.is_empty() && !protocols.contains(&"binary") {
        return (400, "unsupported websocket protocol", None);
    }
    let protocol = if protocols.is_empty() {
        None
    } else {
        Some("binary")
    };
    (0, "", protocol)
}

/// `display_proxy.py::accept_key`: RFC 6455's SHA-1 challenge.
fn accept_key(key: &str) -> String {
    base64_encode(&sha1(format!("{key}{WS_GUID}").as_bytes()))
}

/// Test seam: the RFC 6455 `Sec-WebSocket-Accept` value for one key.
pub fn accept_key_for_tests(key: &str) -> String {
    accept_key(key)
}

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut state: [u32; 5] = [
        0x6745_2301,
        0xEFCD_AB89,
        0x98BA_DCFE,
        0x1032_5476,
        0xC3D2_E1F0,
    ];
    let bit_length = (data.len() as u64).wrapping_mul(8);
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_length.to_be_bytes());
    for chunk in message.chunks(64) {
        let mut words = [0u32; 80];
        for (index, word) in words.iter_mut().take(16).enumerate() {
            *word = u32::from_be_bytes([
                chunk[index * 4],
                chunk[index * 4 + 1],
                chunk[index * 4 + 2],
                chunk[index * 4 + 3],
            ]);
        }
        for index in 16..80 {
            words[index] =
                (words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16])
                    .rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = state;
        for (index, word) in words.iter().enumerate() {
            let (function, constant) = match index {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let temporary = a
                .rotate_left(5)
                .wrapping_add(function)
                .wrapping_add(e)
                .wrapping_add(constant)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temporary;
        }
        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
    }
    let mut digest = [0u8; 20];
    for (index, word) in state.iter().enumerate() {
        digest[index * 4..index * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    digest
}

const BASE64_ALPHABET: &[u8; 64] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn base64_encode(data: &[u8]) -> String {
    let mut output = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (first << 16) | (second << 8) | third;
        output.push(BASE64_ALPHABET[(triple >> 18) as usize & 0x3F] as char);
        output.push(BASE64_ALPHABET[(triple >> 12) as usize & 0x3F] as char);
        if chunk.len() > 1 {
            output.push(BASE64_ALPHABET[(triple >> 6) as usize & 0x3F] as char);
        } else {
            output.push('=');
        }
        if chunk.len() > 2 {
            output.push(BASE64_ALPHABET[triple as usize & 0x3F] as char);
        } else {
            output.push('=');
        }
    }
    output
}

fn base64_decode(value: &str) -> Option<Vec<u8>> {
    let bytes = value.as_bytes();
    if bytes.len() % 4 != 0 {
        return None;
    }
    let mut output = Vec::with_capacity(bytes.len() / 4 * 3);
    for (index, chunk) in bytes.chunks(4).enumerate() {
        let last = index + 1 == bytes.len() / 4;
        let mut values = [0u32; 4];
        let mut padding = 0;
        for (offset, byte) in chunk.iter().enumerate() {
            if *byte == b'=' {
                if offset < 2 || !last {
                    return None;
                }
                padding += 1;
                values[offset] = 0;
            } else {
                if padding > 0 {
                    return None;
                }
                values[offset] = decode_base64_byte(*byte)? as u32;
            }
        }
        let triple = (values[0] << 18) | (values[1] << 12) | (values[2] << 6) | values[3];
        output.push((triple >> 16) as u8);
        if padding < 2 {
            output.push((triple >> 8) as u8);
        }
        if padding < 1 {
            output.push(triple as u8);
        }
    }
    Some(output)
}

fn decode_base64_byte(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

// VNC's d3des uses the standard DES tables with the key bytes in reverse bit
// order (bytebit = 01, 02, 04, ... instead of 80, 40, 20, ...). The password
// file created by `x11vnc -storepasswd` is the 8-byte password encrypted with
// the fixed vnckey below; the challenge response is the 16-byte challenge
// encrypted in two ECB blocks with the password as key. This mirrors the pure
// Python implementation, so the console needs no platform crypto package.
const VNC_FIXED_KEY: [u8; 8] = [23, 82, 107, 6, 35, 78, 88, 7];
#[cfg(unix)]
const VNC_AUTH_FILE_MODES: [u32; 4] = [0o400, 0o440, 0o600, 0o640];

const DES_IP: [u8; 64] = [
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
    64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61,
    53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
];
const DES_FP: [u8; 64] = [
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
    37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
];
const DES_E: [u8; 48] = [
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18,
    19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
];
const DES_P: [u8; 32] = [
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19,
    13, 30, 6, 22, 11, 4, 25,
];
const DES_PC1: [u8; 56] = [
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60,
    52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29,
    21, 13, 5, 28, 20, 12, 4,
];
const DES_PC2: [u8; 48] = [
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2, 41, 52,
    31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
];
const DES_SHIFTS: [u8; 16] = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1];
const DES_SBOX: [[u8; 64]; 8] = [
    [
        14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12,
        11, 9, 5, 3, 8, 4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9,
        1, 7, 5, 11, 3, 14, 10, 0, 6, 13,
    ],
    [
        15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1,
        10, 6, 9, 11, 5, 0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15,
        4, 2, 11, 6, 7, 12, 0, 5, 14, 9,
    ],
    [
        10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5,
        14, 12, 11, 15, 1, 13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6,
        9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12,
    ],
    [
        7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2,
        12, 1, 10, 14, 9, 10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1,
        13, 8, 9, 4, 5, 11, 12, 7, 2, 14,
    ],
    [
        2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15,
        10, 3, 9, 8, 6, 4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14,
        2, 13, 6, 15, 0, 9, 10, 4, 5, 3,
    ],
    [
        12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13,
        14, 0, 11, 3, 8, 9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5,
        15, 10, 11, 14, 1, 7, 6, 0, 8, 13,
    ],
    [
        4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5,
        12, 2, 15, 8, 6, 1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4,
        10, 7, 9, 5, 0, 15, 14, 2, 3, 12,
    ],
    [
        13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6,
        11, 0, 14, 9, 2, 7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10,
        8, 13, 15, 12, 9, 0, 3, 5, 6, 11,
    ],
];

fn des_permute(value: u64, table: &[u8], width: u32) -> u64 {
    let mut output = 0u64;
    for position in table {
        output = (output << 1) | ((value >> (width - u32::from(*position))) & 1);
    }
    output
}

fn des_key_schedule(key: [u8; 8]) -> [u64; 16] {
    let permuted = des_permute(u64::from_be_bytes(key), &DES_PC1, 64);
    let mut left = (permuted >> 28) & 0x0FFF_FFFF;
    let mut right = permuted & 0x0FFF_FFFF;
    let mut keys = [0u64; 16];
    for (index, shift) in DES_SHIFTS.iter().enumerate() {
        let shift = u32::from(*shift);
        left = ((left << shift) | (left >> (28 - shift))) & 0x0FFF_FFFF;
        right = ((right << shift) | (right >> (28 - shift))) & 0x0FFF_FFFF;
        keys[index] = des_permute((left << 28) | right, &DES_PC2, 56);
    }
    keys
}

fn des_block(block: [u8; 8], key: [u8; 8], decrypt: bool) -> [u8; 8] {
    let keys = des_key_schedule(key);
    let value = des_permute(u64::from_be_bytes(block), &DES_IP, 64);
    let mut left = (value >> 32) & 0xFFFF_FFFF;
    let mut right = value & 0xFFFF_FFFF;
    for index in 0..16 {
        let key_part = if decrypt {
            keys[15 - index]
        } else {
            keys[index]
        };
        let expanded = des_permute(right, &DES_E, 32);
        let mixed = expanded ^ key_part;
        let mut substituted = 0u64;
        for position in 0..8 {
            let chunk = (mixed >> (42 - 6 * position)) & 0x3F;
            let row = ((chunk & 0x20) >> 4) | (chunk & 1);
            let column = (chunk >> 1) & 0x0F;
            substituted = (substituted << 4)
                | u64::from(DES_SBOX[position as usize][(row * 16 + column) as usize]);
        }
        let next = left ^ des_permute(substituted, &DES_P, 32);
        left = right;
        right = next;
    }
    des_permute((right << 32) | left, &DES_FP, 64).to_be_bytes()
}

fn vnc_key(key: [u8; 8]) -> [u8; 8] {
    let mut output = [0u8; 8];
    for (index, byte) in key.iter().enumerate() {
        output[index] = byte.reverse_bits();
    }
    output
}

fn des_encrypt_block(block: [u8; 8], key: [u8; 8]) -> [u8; 8] {
    des_block(block, key, false)
}

fn des_decrypt_block(block: [u8; 8], key: [u8; 8]) -> [u8; 8] {
    des_block(block, key, true)
}

fn vnc_password_key(file_bytes: &[u8]) -> Result<[u8; 8], VncAuthError> {
    if file_bytes.len() != 8 {
        return Err(VncAuthError::new("display_vnc_auth_invalid"));
    }
    let block: [u8; 8] = file_bytes
        .try_into()
        .map_err(|_| VncAuthError::new("display_vnc_auth_invalid"))?;
    Ok(des_decrypt_block(block, vnc_key(VNC_FIXED_KEY)))
}

fn vnc_auth_response(password: &[u8], challenge: &[u8]) -> Result<[u8; 16], VncAuthError> {
    if password.len() != 8 || challenge.len() != 16 {
        return Err(VncAuthError::new("display_vnc_auth_invalid"));
    }
    let password: [u8; 8] = password
        .try_into()
        .map_err(|_| VncAuthError::new("display_vnc_auth_invalid"))?;
    let key = vnc_key(password);
    let mut response = [0u8; 16];
    let first: [u8; 8] = challenge[..8]
        .try_into()
        .map_err(|_| VncAuthError::new("display_vnc_auth_invalid"))?;
    let second: [u8; 8] = challenge[8..]
        .try_into()
        .map_err(|_| VncAuthError::new("display_vnc_auth_invalid"))?;
    response[..8].copy_from_slice(&des_encrypt_block(first, key));
    response[8..].copy_from_slice(&des_encrypt_block(second, key));
    Ok(response)
}

/// Test seam: the standard DES block encryption used by VNC auth.
pub fn des_encrypt_block_for_tests(block: [u8; 8], key: [u8; 8]) -> [u8; 8] {
    des_encrypt_block(block, key)
}

/// Test seam: the 16-byte VncAuth challenge response.
pub fn vnc_auth_response_for_tests(
    password: &[u8],
    challenge: &[u8],
) -> Result<[u8; 16], VncAuthError> {
    vnc_auth_response(password, challenge)
}

/// Test seam: decrypt one 8-byte `x11vnc -storepasswd` file payload.
pub fn vnc_password_key_for_tests(file_bytes: &[u8]) -> Result<[u8; 8], VncAuthError> {
    vnc_password_key(file_bytes)
}

/// Test seam: read and decrypt one `x11vnc -storepasswd` file.
pub fn load_vnc_password_file_for_tests(path: &Path) -> Result<[u8; 8], VncAuthError> {
    let password = vnc_password_key(&read_password_file(path)?)?;
    Ok(password)
}

fn read_password_file(path: &Path) -> Result<[u8; 8], VncAuthError> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = options
        .open(path)
        .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    let metadata = file
        .metadata()
        .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    if !metadata.is_file() || metadata.len() != 8 {
        return Err(VncAuthError::new("display_vnc_auth_invalid"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if !VNC_AUTH_FILE_MODES.contains(&(metadata.mode() & 0o777)) {
            return Err(VncAuthError::new("display_vnc_auth_permissions"));
        }
        let uid = unsafe { libc::getuid() };
        if metadata.uid() != uid && metadata.uid() != 0 {
            return Err(VncAuthError::new("display_vnc_auth_owner"));
        }
    }
    let mut raw = [0u8; 8];
    file.read_exact(&mut raw)
        .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    Ok(raw)
}

/// Challenge-response provider backed by the root-only password file.
pub struct PasswordFileVncAuth {
    path: PathBuf,
}

impl PasswordFileVncAuth {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }
}

impl VncAuthProvider for PasswordFileVncAuth {
    fn response(&self, challenge: &[u8; 16]) -> Result<[u8; 16], VncAuthError> {
        let password = vnc_password_key(&read_password_file(&self.path)?)?;
        vnc_auth_response(&password, challenge)
    }
}

/// Ask the display service's narrow helper for a challenge response.
///
/// The helper is the only process that reads the credential; the console sees
/// only a 16-byte challenge in and a 16-byte response out.
pub struct UnixVncAuthClient {
    path: String,
    timeout: Duration,
}

impl UnixVncAuthClient {
    pub fn new(path: &str, timeout: Duration) -> Self {
        Self {
            path: path.to_string(),
            timeout,
        }
    }
}

impl VncAuthProvider for UnixVncAuthClient {
    fn response(&self, challenge: &[u8; 16]) -> Result<[u8; 16], VncAuthError> {
        #[cfg(unix)]
        {
            use std::os::unix::net::UnixStream;
            let mut stream = UnixStream::connect(&self.path)
                .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
            stream
                .set_read_timeout(Some(self.timeout))
                .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
            stream
                .set_write_timeout(Some(self.timeout))
                .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
            stream
                .write_all(challenge)
                .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
            let mut response = [0u8; 16];
            stream
                .read_exact(&mut response)
                .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
            Ok(response)
        }
        #[cfg(not(unix))]
        {
            let _ = (challenge, &self.path, self.timeout);
            Err(VncAuthError::new("display_vnc_auth_unavailable"))
        }
    }
}

fn build_auth_provider(config: &DisplayConfig) -> Option<Arc<dyn VncAuthProvider>> {
    if !config.vnc_auth_socket.is_empty() {
        Some(Arc::new(UnixVncAuthClient::new(
            &config.vnc_auth_socket,
            config.handshake_timeout,
        )))
    } else if !config.vnc_auth_file.is_empty() {
        Some(Arc::new(PasswordFileVncAuth::new(&config.vnc_auth_file)))
    } else {
        None
    }
}

/// `display_proxy.py::perform_vnc_auth`: complete the upstream RFB security
/// handshake and return the browser greeting frames.
fn perform_vnc_auth(
    upstream: Option<TcpStream>,
    provider: &dyn VncAuthProvider,
    timeout: Duration,
) -> Result<Vec<Vec<u8>>, VncAuthError> {
    let mut upstream = upstream.ok_or_else(|| VncAuthError::new("display_vnc_auth_unavailable"))?;
    let _ = upstream.set_read_timeout(Some(timeout));
    let version = recv_exact(&mut upstream, 12)
        .map_err(|_| VncAuthError::new("display_vnc_auth_protocol"))?;
    if !version.starts_with(b"RFB ") || !version.ends_with(b"\n") {
        return Err(VncAuthError::new("display_vnc_auth_protocol"));
    }
    let major = std::str::from_utf8(&version[4..7])
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .ok_or_else(|| VncAuthError::new("display_vnc_auth_protocol"))?;
    let minor = std::str::from_utf8(&version[8..11])
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .ok_or_else(|| VncAuthError::new("display_vnc_auth_protocol"))?;
    if !matches!((major, minor), (3, 3) | (3, 7) | (3, 8)) {
        return Err(VncAuthError::new("display_vnc_auth_protocol"));
    }
    let negotiated = if (major, minor) >= (3, 8) {
        b"RFB 003.008\n".to_vec()
    } else if (major, minor) == (3, 7) {
        b"RFB 003.007\n".to_vec()
    } else {
        b"RFB 003.003\n".to_vec()
    };
    send_all(&mut upstream, &negotiated)?;
    let types = read_security_types(&mut upstream, major, minor)?;
    if types.contains(&2) {
        send_all(&mut upstream, b"\x02")?;
        let challenge = recv_exact(&mut upstream, 16)
            .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
        let challenge: [u8; 16] = challenge
            .try_into()
            .map_err(|_| VncAuthError::new("display_vnc_auth_invalid"))?;
        let response = provider.response(&challenge)?;
        send_all(&mut upstream, &response)?;
        read_security_result(&mut upstream, major, minor)?;
    } else if types.contains(&1) {
        if (major, minor) != (3, 3) {
            read_security_result(&mut upstream, major, minor)?;
        }
    } else {
        return Err(VncAuthError::new("display_vnc_auth_unsupported"));
    }
    send_all(&mut upstream, b"\x01")?;
    let server_init = read_server_init(&mut upstream)?;
    Ok(vec![
        b"RFB 003.008\n".to_vec(),
        b"\x01\x01".to_vec(),
        b"\x00\x00\x00\x00".to_vec(),
        server_init,
    ])
}

fn read_security_types(
    upstream: &mut TcpStream,
    major: u32,
    minor: u32,
) -> Result<Vec<u8>, VncAuthError> {
    if (major, minor) >= (3, 7) {
        let count = recv_exact(upstream, 1)
            .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?[0];
        if count == 0 {
            return Err(VncAuthError::new("display_vnc_auth_failed"));
        }
        recv_exact(upstream, count as usize)
            .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))
    } else {
        let value = recv_exact(upstream, 4)
            .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
        Ok(vec![
            u32::from_be_bytes([value[0], value[1], value[2], value[3]]) as u8,
        ])
    }
}

fn read_security_result(
    upstream: &mut TcpStream,
    major: u32,
    minor: u32,
) -> Result<(), VncAuthError> {
    let result =
        recv_exact(upstream, 4).map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    if u32::from_be_bytes([result[0], result[1], result[2], result[3]]) == 0 {
        return Ok(());
    }
    if (major, minor) >= (3, 8) {
        let length = recv_exact(upstream, 4)
            .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
        let length = u32::from_be_bytes([length[0], length[1], length[2], length[3]]);
        if length > 0 && length <= 4096 {
            let _ = recv_exact(upstream, length as usize);
        }
    }
    Err(VncAuthError::new("display_vnc_auth_failed"))
}

fn read_server_init(upstream: &mut TcpStream) -> Result<Vec<u8>, VncAuthError> {
    let head =
        recv_exact(upstream, 24).map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    let name_length = u32::from_be_bytes([head[20], head[21], head[22], head[23]]) as usize;
    if name_length > 4096 {
        return Err(VncAuthError::new("display_vnc_auth_protocol"));
    }
    let name = recv_exact(upstream, name_length)
        .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))?;
    let mut init = head;
    init.extend_from_slice(&name);
    Ok(init)
}

fn recv_exact(stream: &mut TcpStream, size: usize) -> std::io::Result<Vec<u8>> {
    let mut data = Vec::with_capacity(size);
    let mut buffer = [0u8; 4096];
    while data.len() < size {
        let remaining = size - data.len();
        let read = stream.read(&mut buffer[..remaining])?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "display peer closed",
            ));
        }
        data.extend_from_slice(&buffer[..read]);
    }
    Ok(data)
}

fn send_all(stream: &mut TcpStream, bytes: &[u8]) -> Result<(), VncAuthError> {
    stream
        .write_all(bytes)
        .and_then(|_| stream.flush())
        .map_err(|_| VncAuthError::new("display_vnc_auth_unavailable"))
}

#[derive(Clone, Copy)]
struct RelayOptions<'a> {
    idle_timeout: Duration,
    max_session_seconds: Duration,
    discard: usize,
    input_allowed: &'a (dyn Fn() -> bool + Sync),
}

struct RelayContext {
    stop: AtomicBool,
    close_sent: AtomicBool,
    started: Instant,
    last_client_activity: Mutex<Instant>,
    writer: Mutex<TcpStream>,
}

impl RelayContext {
    fn stopped(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }

    fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    fn note_activity(&self) {
        *self
            .last_client_activity
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Instant::now();
    }

    fn last_activity(&self) -> Instant {
        *self
            .last_client_activity
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn send(&self, opcode: u8, payload: &[u8]) -> std::io::Result<()> {
        let mut writer = self
            .writer
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        send_frame(&mut writer, opcode, payload, Some(SEND_FRAME_TIMEOUT))
    }

    /// Best-effort close frame, bounded so it cannot block termination.
    fn send_close(&self, code: u16) {
        if self.close_sent.swap(true, Ordering::SeqCst) {
            return;
        }
        let Ok(mut writer) = self.writer.try_lock() else {
            return;
        };
        let _ = send_frame(
            &mut writer,
            0x08,
            &code.to_be_bytes(),
            Some(CLOSE_FRAME_TIMEOUT),
        );
        let _ = writer.shutdown(Shutdown::Write);
    }
}

fn send_frame(
    stream: &mut TcpStream,
    opcode: u8,
    payload: &[u8],
    timeout: Option<Duration>,
) -> std::io::Result<()> {
    let length = payload.len();
    let mut frame = Vec::with_capacity(length + 10);
    frame.push(0x80 | opcode);
    if length < 126 {
        frame.push(length as u8);
    } else if length <= 0xFFFF {
        frame.push(126);
        frame.extend_from_slice(&(length as u16).to_be_bytes());
    } else {
        frame.push(127);
        frame.extend_from_slice(&(length as u64).to_be_bytes());
    }
    frame.extend_from_slice(payload);
    if let Some(timeout) = timeout {
        let _ = stream.set_write_timeout(Some(timeout));
    }
    stream.write_all(&frame)?;
    stream.flush()
}

#[derive(Debug)]
struct Frame {
    opcode: u8,
    payload: Vec<u8>,
}

#[derive(Debug)]
enum FrameError {
    Timeout,
    Eof,
    Protocol,
}

struct FrameReader {
    stream: TcpStream,
    buffer: Vec<u8>,
}

impl FrameReader {
    fn new(stream: TcpStream, initial: Vec<u8>) -> Self {
        Self {
            stream,
            buffer: initial,
        }
    }

    /// Wait for the first byte of a frame without a timeout; the stop flag
    /// ends the wait. Once a frame has started its remainder is bounded by
    /// `CLIENT_FRAME_TIMEOUT`.
    fn wait_for_data(&mut self, stop: &AtomicBool) -> bool {
        if !self.buffer.is_empty() {
            return true;
        }
        let mut byte = [0u8; 1];
        loop {
            if stop.load(Ordering::SeqCst) {
                return false;
            }
            match self.stream.peek(&mut byte) {
                Ok(0) => return false,
                Ok(_) => return true,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) =>
                {
                    continue;
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return false,
            }
        }
    }

    fn read_exact(&mut self, size: usize) -> Result<Vec<u8>, FrameError> {
        let _ = self.stream.set_read_timeout(Some(CLIENT_FRAME_TIMEOUT));
        let mut chunk = [0u8; 4096];
        while self.buffer.len() < size {
            match self.stream.read(&mut chunk) {
                Ok(0) => return Err(FrameError::Eof),
                Ok(read) => self.buffer.extend_from_slice(&chunk[..read]),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) =>
                {
                    return Err(FrameError::Timeout);
                }
                Err(_) => return Err(FrameError::Eof),
            }
        }
        let data = self.buffer[..size].to_vec();
        self.buffer.drain(..size);
        Ok(data)
    }

    fn read_frame(&mut self) -> Result<Frame, FrameError> {
        let head = self.read_exact(2)?;
        let first = head[0];
        let second = head[1];
        let final_frame = first & 0x80 != 0;
        let opcode = first & 0x0F;
        let masked = second & 0x80 != 0;
        let mut length = u64::from(second & 0x7F);
        if first & 0x70 != 0 {
            return Err(FrameError::Protocol);
        }
        if !masked {
            return Err(FrameError::Protocol);
        }
        if length == 126 {
            let extended = self.read_exact(2)?;
            length = u64::from(u16::from_be_bytes([extended[0], extended[1]]));
            if length < 126 {
                return Err(FrameError::Protocol);
            }
        } else if length == 127 {
            let extended = self.read_exact(8)?;
            length = u64::from_be_bytes(extended.try_into().unwrap_or_default());
            if length <= 0xFFFF || length >> 63 != 0 {
                return Err(FrameError::Protocol);
            }
        }
        if length > MAX_FRAME_PAYLOAD as u64 {
            return Err(FrameError::Protocol);
        }
        if opcode & 0x08 != 0 && (!final_frame || length > 125) {
            return Err(FrameError::Protocol);
        }
        let mask = self.read_exact(4)?;
        let mut payload = self.read_exact(length as usize)?;
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % 4];
        }
        Ok(Frame { opcode, payload })
    }
}

fn close_code(payload: &[u8]) -> u16 {
    if payload.len() < 2 {
        return 1000;
    }
    let code = u16::from_be_bytes([payload[0], payload[1]]);
    if (1000..=4999).contains(&code) && ![1004, 1005, 1006, 1015].contains(&code) {
        code
    } else {
        1002
    }
}

fn run_relay(
    client: &mut TcpStream,
    initial: Vec<u8>,
    upstream: TcpStream,
    options: RelayOptions<'_>,
) {
    let Ok(client_reader) = client.try_clone() else {
        return;
    };
    let Ok(client_writer) = client.try_clone() else {
        return;
    };
    let Ok(upstream_reader) = upstream.try_clone() else {
        return;
    };
    let Ok(upstream_writer) = upstream.try_clone() else {
        return;
    };
    let context = Arc::new(RelayContext {
        stop: AtomicBool::new(false),
        close_sent: AtomicBool::new(false),
        started: Instant::now(),
        last_client_activity: Mutex::new(Instant::now()),
        writer: Mutex::new(client_writer),
    });
    let client_context = Arc::clone(&context);
    let upstream_context = Arc::clone(&context);
    thread::scope(|scope| {
        scope.spawn(move || {
            client_loop(
                client_reader,
                upstream_writer,
                initial,
                options.discard,
                options.input_allowed,
                client_context,
            );
        });
        scope.spawn(move || {
            upstream_loop(upstream_reader, upstream_context);
        });
        watchdog(&context, &options);
    });
    context.stop();
    let _ = client.shutdown(Shutdown::Both);
    let _ = upstream.shutdown(Shutdown::Both);
}

fn client_loop(
    stream: TcpStream,
    mut upstream: TcpStream,
    initial: Vec<u8>,
    discard: usize,
    input_allowed: &(dyn Fn() -> bool + Sync),
    context: Arc<RelayContext>,
) {
    let mut reader = FrameReader::new(stream, initial);
    let mut remaining = discard;
    loop {
        if context.stopped() || !reader.wait_for_data(&context.stop) {
            break;
        }
        let frame = match reader.read_frame() {
            // debug
            // debug below
            Ok(frame) => frame,
            Err(FrameError::Timeout) => {
                context.send_close(1002);
                break;
            }
            Err(FrameError::Eof) => {
                context.send_close(1000);
                break;
            }
            Err(FrameError::Protocol) => {
                context.send_close(1002);
                break;
            }
        };
        match frame.opcode {
            0x08 => {
                context.send_close(close_code(&frame.payload));
                break;
            }
            0x09 => {
                if context.send(0x0A, &frame.payload).is_err() {
                    break;
                }
                continue;
            }
            0x0A => continue,
            0x02 => {}
            _ => {
                context.send_close(1003);
                break;
            }
        }
        let mut payload = frame.payload;
        if !payload.is_empty() {
            context.note_activity();
        }
        if !payload.is_empty() && remaining > 0 {
            let drop = payload.len().min(remaining);
            remaining -= drop;
            payload.drain(..drop);
            if payload.is_empty() {
                continue;
            }
        }
        if !payload.is_empty() && matches!(payload[0], 4 | 5) && !input_allowed() {
            continue;
        }
        if !payload.is_empty() && upstream.write_all(&payload).is_err() {
            context.send_close(1000);
            break;
        }
    }
    context.stop();
}

fn upstream_loop(mut upstream: TcpStream, context: Arc<RelayContext>) {
    let _ = upstream.set_read_timeout(Some(Duration::from_millis(500)));
    let mut buffer = [0u8; UPSTREAM_READ_SIZE];
    loop {
        if context.stopped() {
            break;
        }
        match upstream.read(&mut buffer) {
            Ok(0) => {
                context.send_close(1000);
                break;
            }
            Ok(read) => {
                if context.send(0x02, &buffer[..read]).is_err() {
                    break;
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) =>
            {
                continue;
            }
            Err(_) => break,
        }
    }
    context.stop();
}

fn watchdog(context: &RelayContext, options: &RelayOptions<'_>) {
    let ping_interval = PING_INTERVAL
        .min(Duration::from_secs_f64(
            options.idle_timeout.as_secs_f64() / 3.0,
        ))
        .max(Duration::from_secs(1));
    let mut last_ping = Instant::now();
    loop {
        if context.stopped() {
            break;
        }
        thread::sleep(RELAY_POLL_INTERVAL);
        let now = Instant::now();
        if now.duration_since(context.started) > options.max_session_seconds
            || now.duration_since(context.last_activity()) > options.idle_timeout
        {
            context.send_close(1000);
            context.stop();
            break;
        }
        if now.duration_since(last_ping) >= ping_interval {
            let mut payload = [0u8; 8];
            let _ = getrandom::getrandom(&mut payload);
            if context.send(0x09, &payload).is_err() {
                context.stop();
                break;
            }
            last_ping = now;
        }
    }
}

fn display_session_valid(grants: &DisplayGrants, cookie: Option<&str>, origin: &str) -> bool {
    let token = cookie.and_then(|header| cookie_value(header, DISPLAY_SESSION_COOKIE));
    grants
        .validate_session(token.as_deref().unwrap_or_default(), origin)
        .is_some()
}

fn display_session(
    grants: &DisplayGrants,
    cookie: Option<&str>,
    origin: &str,
) -> Option<crate::display_grant::DisplaySession> {
    let token = cookie.and_then(|header| cookie_value(header, DISPLAY_SESSION_COOKIE));
    grants.validate_session(token.as_deref().unwrap_or_default(), origin)
}

fn cookie_value(header: &str, name: &str) -> Option<String> {
    for part in header.split(';') {
        let Some((key, value)) = part.trim().split_once('=') else {
            continue;
        };
        if key == name {
            return Some(value.trim_matches('"').to_string());
        }
    }
    None
}

/// `display_proxy.py::_header`: exactly one occurrence, otherwise absent.
fn header<'a>(headers: &'a Headers, name: &str) -> Option<&'a str> {
    let values = headers.get_all(name);
    if values.len() == 1 {
        Some(values[0])
    } else {
        None
    }
}

/// `display_proxy.py::_origin_checked` for the local (non-Tailscale) case.
/// This candidate assembles no Tailscale Serve policy, so any proxy metadata
/// fails closed instead of being trusted.
fn origin_checked(request: &Request, console_port: u16, websocket: bool) -> Option<String> {
    if has_proxy_metadata(&request.headers) {
        return None;
    }
    if !request.peer_ip.is_loopback() {
        return None;
    }
    origin_for_local_request(request, console_port, websocket)
}

/// `display_proxy.py::_origin_for_local_request`.
fn origin_for_local_request(
    request: &Request,
    console_port: u16,
    websocket: bool,
) -> Option<String> {
    let host = header(&request.headers, "Host")?;
    if host
        .chars()
        .any(|character| "/\\?#@ \t\r\n".contains(character))
    {
        return None;
    }
    let (hostname, port) = parse_authority(host)?;
    if !matches!(hostname.as_str(), "127.0.0.1" | "localhost" | "::1") {
        return None;
    }
    if port != console_port {
        return None;
    }
    let expected = format!("http://{host}");
    match header(&request.headers, "Origin") {
        None if websocket => None,
        None => Some(expected),
        Some(origin) if origin == expected => Some(expected),
        Some(_) => None,
    }
}

fn parse_authority(host: &str) -> Option<(String, u16)> {
    if let Some(rest) = host.strip_prefix('[') {
        let (inside, tail) = rest.split_once(']')?;
        let port = tail.strip_prefix(':')?;
        let port = port.parse::<u16>().ok()?;
        return Some((inside.to_ascii_lowercase(), port));
    }
    let (name, port) = host.rsplit_once(':')?;
    if name.contains(':') {
        return None;
    }
    let port = port.parse::<u16>().ok()?;
    Some((name.to_ascii_lowercase(), port))
}

/// `console/tailnet.py::has_proxy_metadata`.
fn has_proxy_metadata(headers: &Headers) -> bool {
    headers.iter().any(|header| {
        let name = header.name.to_ascii_lowercase();
        name.starts_with("tailscale-") || name.starts_with("x-forwarded-") || name == "forwarded"
    })
}

/// `display_proxy.py::_safe_asset`.
fn safe_asset(relative: &str) -> bool {
    if relative.is_empty()
        || relative
            .chars()
            .any(|character| matches!(character, '\\' | '%' | '\0' | ':'))
    {
        return false;
    }
    if relative
        .split('/')
        .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return false;
    }
    static_type(relative).is_some()
}

fn static_type(relative: &str) -> Option<&'static str> {
    let extension = Path::new(relative)
        .extension()
        .and_then(|extension| extension.to_str())?
        .to_ascii_lowercase();
    STATIC_TYPES
        .iter()
        .find(|(suffix, _)| suffix.trim_start_matches('.') == extension)
        .map(|(_, content_type)| *content_type)
}

fn split_target(raw: &str) -> (&str, Option<&str>, bool) {
    let (without_fragment, fragment) = match raw.split_once('#') {
        Some((head, _)) => (head, true),
        None => (raw, false),
    };
    match without_fragment.split_once('?') {
        Some((path, query)) => (path, Some(query), fragment),
        None => (without_fragment, None, fragment),
    }
}

fn query_pairs(query: &str) -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    for part in query.split('&') {
        if part.is_empty() {
            continue;
        }
        let (key, value) = part.split_once('=').unwrap_or((part, ""));
        pairs.push((decode_query_part(key), decode_query_part(value)));
    }
    pairs
}

fn decode_query_part(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let high = (bytes[index + 1] as char).to_digit(16);
                let low = (bytes[index + 2] as char).to_digit(16);
                match (high, low) {
                    (Some(high), Some(low)) => {
                        output.push((high * 16 + low) as u8);
                        index += 3;
                    }
                    _ => {
                        output.push(b'%');
                        index += 1;
                    }
                }
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&output).to_string()
}

/// `display_proxy.py::_operator_generation`: canonical positive decimal.
fn parse_operator_generation(value: &str) -> Option<u64> {
    if value.is_empty() || value.len() > 19 || value.starts_with('0') {
        return None;
    }
    if !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let generation = value.parse::<u64>().ok()?;
    (generation > 0).then_some(generation)
}

fn display_json(status: u16, error: &str) -> Response {
    let mut response = Response::json(status, &json!({"error": error}));
    for (name, value) in SECURITY_HEADERS {
        response = response.with_header(name, value);
    }
    response.close = true;
    response
}

fn display_json_body(payload: Value) -> Response {
    let mut response = Response::json(200, &payload);
    for (name, value) in SECURITY_HEADERS {
        response = response.with_header(name, value);
    }
    response.close = true;
    response
}

fn display_empty(status: u16, headers: &[(&str, &str)]) -> Response {
    let mut response = Response::new(status, Vec::new()).with_header("Cache-Control", "no-store");
    for (name, value) in headers {
        response = response.with_header(*name, *value);
    }
    response.close = true;
    response
}

fn epoch_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}
