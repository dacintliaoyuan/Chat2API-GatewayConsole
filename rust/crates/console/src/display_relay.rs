//! S7: the private noVNC display surface.
//!
//! This is `console/display_proxy.py`: the launcher page, its module assets,
//! the read-only noVNC tree, one-time display grants and the WebSocket-to-RFB
//! relay. The candidate assembles only a loopback listener; the fake x11vnc
//! used by tests is never the real display on 8097.

use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};

use um_gateway::http::{BodyRead, Headers, Request, Response};

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

pub struct DisplayRelay {
    config: DisplayConfig,
}

impl DisplayRelay {
    pub fn new(config: DisplayConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &DisplayConfig {
        &self.config
    }

    pub fn active_connections(&self) -> usize {
        0
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
        let origin = self.request_origin(
            request,
            console_port,
            false,
            false,
            grants,
            session,
            control_token_file,
        )?;
        Some(self.serve_grant_exchange(request, origin, grants))
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
