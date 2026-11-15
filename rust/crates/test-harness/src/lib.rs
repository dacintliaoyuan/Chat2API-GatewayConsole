//! S0 test harness for the Rust rewrite.
//!
//! This crate is a *test-only* boundary. It never binds a governed production
//! port and never reads production data:
//!
//! * [`TestServer`] binds an ephemeral loopback port (`127.0.0.1:0`) unless the
//!   caller explicitly supplies a port, and [`HarnessConfig::forbidden_ports`]
//!   fails closed before a production port is bound.
//! * [`TempDataDir`] creates its own directory under the OS temp root, rejects
//!   path traversal in helpers, and removes the tree on [`TempDataDir::close`] or
//!   drop.
//!
//! The HTTP implementation is deliberately small and synchronous: it exists to
//! prove that a candidate process can be started, addressed and observed without a
//! production dependency, not to be the production HTTP stack.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

static NEXT_TEMP_DIR: AtomicU64 = AtomicU64::new(0);

/// A harness failure that must be visible to the caller.
#[derive(Debug)]
pub enum HarnessError {
    /// A filesystem or socket operation failed.
    Io(std::io::Error),
    /// The configured bind host is not loopback.
    NonLoopbackHost(String),
    /// The configured port is a governed production port.
    ProductionPort(u16),
    /// A helper was asked to resolve a path outside the temp data dir.
    InvalidRelativePath(PathBuf),
    /// The harness received a response it could not parse.
    InvalidHttpResponse(&'static str),
    /// The server thread panicked or could not be joined.
    ServerThread,
}

impl fmt::Display for HarnessError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "I/O error: {error}"),
            Self::NonLoopbackHost(host) => {
                write!(formatter, "bind host is not loopback: {host}")
            }
            Self::ProductionPort(port) => {
                write!(
                    formatter,
                    "refusing to bind governed production port {port}"
                )
            }
            Self::InvalidRelativePath(path) => {
                write!(
                    formatter,
                    "path escapes the temp data dir: {}",
                    path.display()
                )
            }
            Self::InvalidHttpResponse(reason) => {
                write!(formatter, "invalid HTTP response: {reason}")
            }
            Self::ServerThread => write!(formatter, "test server thread failed"),
        }
    }
}

impl std::error::Error for HarnessError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<std::io::Error> for HarnessError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

/// Configuration for a harness HTTP server.
#[derive(Debug, Clone)]
pub struct HarnessConfig {
    /// Bind host. Must be a loopback literal; the default is `127.0.0.1`.
    pub bind_host: String,
    /// Bind port. `0` asks the OS for an ephemeral port.
    pub bind_port: u16,
    /// Ports that must never be bound by this harness.
    pub forbidden_ports: Vec<u16>,
}

impl Default for HarnessConfig {
    fn default() -> Self {
        Self {
            bind_host: "127.0.0.1".to_string(),
            bind_port: 0,
            forbidden_ports: Vec::new(),
        }
    }
}

impl HarnessConfig {
    fn validate(&self) -> Result<(), HarnessError> {
        let host = self.bind_host.as_str();
        if !(host == "127.0.0.1" || host == "::1" || host == "localhost") {
            return Err(HarnessError::NonLoopbackHost(self.bind_host.clone()));
        }
        if self.forbidden_ports.contains(&self.bind_port) {
            return Err(HarnessError::ProductionPort(self.bind_port));
        }
        Ok(())
    }
}

/// One parsed HTTP request observed by the harness.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpRequest {
    pub method: String,
    pub path: String,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl HttpRequest {
    pub fn new(method: impl Into<String>, path: impl Into<String>) -> Self {
        Self {
            method: method.into(),
            path: path.into(),
            headers: BTreeMap::new(),
            body: Vec::new(),
        }
    }

    pub fn get(path: impl Into<String>) -> Self {
        Self::new("GET", path)
    }

    pub fn post(path: impl Into<String>, body: impl Into<Vec<u8>>) -> Self {
        Self {
            method: "POST".to_string(),
            path: path.into(),
            headers: BTreeMap::new(),
            body: body.into(),
        }
    }

    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers
            .insert(name.into().to_ascii_lowercase(), value.into());
        self
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .get(&name.to_ascii_lowercase())
            .map(String::as_str)
    }
}

/// One HTTP response produced by a harness handler.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Self {
        Self {
            status,
            headers: BTreeMap::new(),
            body: body.into(),
        }
    }

    pub fn text(status: u16, body: &str) -> Self {
        Self::new(status, body.as_bytes().to_vec())
            .with_header("content-type", "text/plain; charset=utf-8")
    }

    pub fn json(status: u16, body: &str) -> Self {
        Self::new(status, body.as_bytes().to_vec()).with_header("content-type", "application/json")
    }

    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers
            .insert(name.into().to_ascii_lowercase(), value.into());
        self
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .get(&name.to_ascii_lowercase())
            .map(String::as_str)
    }
}

/// A harness HTTP server bound to loopback.
#[derive(Debug)]
pub struct TestServer {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl TestServer {
    /// Start a server on an OS-assigned ephemeral loopback port.
    pub fn start<F>(handler: F) -> Result<Self, HarnessError>
    where
        F: Fn(HttpRequest) -> HttpResponse + Send + Sync + 'static,
    {
        Self::start_with(HarnessConfig::default(), handler)
    }

    /// Start a server with an explicit harness configuration.
    pub fn start_with<F>(config: HarnessConfig, handler: F) -> Result<Self, HarnessError>
    where
        F: Fn(HttpRequest) -> HttpResponse + Send + Sync + 'static,
    {
        config.validate()?;
        let listener = TcpListener::bind((config.bind_host.as_str(), config.bind_port))?;
        listener.set_nonblocking(true)?;
        let addr = listener.local_addr()?;
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_shutdown = Arc::clone(&shutdown);
        let handler = Arc::new(handler);
        let handle = thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let _ = serve(stream, handler.as_ref());
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            addr,
            shutdown,
            handle: Some(handle),
        })
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn host(&self) -> String {
        self.addr.ip().to_string()
    }

    pub fn port(&self) -> u16 {
        self.addr.port()
    }

    pub fn url(&self) -> String {
        format!("http://{}/", self.addr)
    }

    /// Stop accepting requests and join the server thread.
    pub fn shutdown(&mut self) -> Result<(), HarnessError> {
        if let Some(handle) = self.handle.take() {
            self.shutdown.store(true, Ordering::SeqCst);
            // Wake a non-blocking accept loop; failure only means the loop is
            // already between accepts, which the join below still handles.
            let _ = TcpStream::connect_timeout(&self.addr, Duration::from_millis(200));
            handle.join().map_err(|_| HarnessError::ServerThread)?;
        }
        Ok(())
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

/// A minimal HTTP/1.1 client for harness requests.
pub struct TestClient;

impl TestClient {
    pub fn request(addr: SocketAddr, request: &HttpRequest) -> Result<HttpResponse, HarnessError> {
        let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2))?;
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        stream.set_write_timeout(Some(Duration::from_secs(5)))?;
        let mut raw = format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n",
            request.method,
            request.path,
            addr,
            request.body.len()
        );
        for (name, value) in &request.headers {
            raw.push_str(name);
            raw.push_str(": ");
            raw.push_str(value);
            raw.push_str("\r\n");
        }
        raw.push_str("\r\n");
        stream.write_all(raw.as_bytes())?;
        stream.write_all(&request.body)?;
        stream.flush()?;
        let mut response = Vec::new();
        stream.read_to_end(&mut response)?;
        HttpResponse::parse(&response)
    }
}

impl HttpResponse {
    fn parse(raw: &[u8]) -> Result<Self, HarnessError> {
        let header_end = raw
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .ok_or(HarnessError::InvalidHttpResponse(
                "missing header terminator",
            ))?;
        let head = std::str::from_utf8(&raw[..header_end])
            .map_err(|_| HarnessError::InvalidHttpResponse("header is not UTF-8"))?;
        let mut lines = head.split("\r\n");
        let status_line = lines
            .next()
            .ok_or(HarnessError::InvalidHttpResponse("missing status line"))?;
        let status = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|value| value.parse::<u16>().ok())
            .ok_or(HarnessError::InvalidHttpResponse("invalid status code"))?;
        let mut headers = BTreeMap::new();
        for line in lines {
            if let Some((name, value)) = line.split_once(':') {
                headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
            }
        }
        Ok(Self {
            status,
            headers,
            body: raw[header_end + 4..].to_vec(),
        })
    }
}

fn serve<F>(mut stream: TcpStream, handler: &F) -> std::io::Result<()>
where
    F: Fn(HttpRequest) -> HttpResponse,
{
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            return Ok(());
        }
        raw.extend_from_slice(&buffer[..read]);
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        if raw.len() > 64 * 1024 {
            return Ok(());
        }
    };

    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("GET").to_string();
    let path = parts.next().unwrap_or("/").to_string();
    let mut headers = BTreeMap::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
        }
    }
    let content_length = headers
        .get("content-length")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0);
    let mut body = raw[header_end..].to_vec();
    while body.len() < content_length {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        body.extend_from_slice(&buffer[..read]);
    }
    if body.len() > content_length {
        body.truncate(content_length);
    }

    let response = handler(HttpRequest {
        method,
        path,
        headers,
        body,
    });
    let mut out = format!(
        "HTTP/1.1 {} {}\r\n",
        response.status,
        reason_phrase(response.status)
    );
    let mut headers = response.headers.clone();
    headers.insert(
        "content-length".to_string(),
        response.body.len().to_string(),
    );
    headers.insert("connection".to_string(), "close".to_string());
    for (name, value) in headers {
        out.push_str(&name);
        out.push_str(": ");
        out.push_str(&value);
        out.push_str("\r\n");
    }
    out.push_str("\r\n");
    stream.write_all(out.as_bytes())?;
    stream.write_all(&response.body)?;
    stream.flush()
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        410 => "Gone",
        413 => "Payload Too Large",
        415 => "Unsupported Media Type",
        426 => "Upgrade Required",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "Unknown",
    }
}

/// A temporary, self-owned data directory for one test.
#[derive(Debug)]
pub struct TempDataDir {
    path: PathBuf,
    keep: bool,
    closed: bool,
}

impl TempDataDir {
    /// Create a new unique directory under the OS temp root.
    pub fn new(prefix: &str) -> Result<Self, HarnessError> {
        if prefix.is_empty()
            || !prefix.chars().all(|character| {
                character.is_ascii_alphanumeric() || character == '-' || character == '_'
            })
        {
            return Err(HarnessError::InvalidRelativePath(PathBuf::from(prefix)));
        }
        let root = std::env::temp_dir();
        for _ in 0..100 {
            let sequence = NEXT_TEMP_DIR.fetch_add(1, Ordering::SeqCst);
            let path = root.join(format!("{prefix}{}-{sequence}", std::process::id()));
            match fs::create_dir(&path) {
                Ok(()) => {
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
                    }
                    return Ok(Self {
                        path,
                        keep: false,
                        closed: false,
                    });
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error.into()),
            }
        }
        Err(HarnessError::Io(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "could not allocate a unique temp data dir",
        )))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Write a file inside the temp data dir, creating parent directories.
    pub fn write(&self, relative: impl AsRef<Path>, bytes: &[u8]) -> Result<PathBuf, HarnessError> {
        let target = self.resolve(relative.as_ref())?;
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&target, bytes)?;
        Ok(target)
    }

    /// Read a file inside the temp data dir.
    pub fn read(&self, relative: impl AsRef<Path>) -> Result<Vec<u8>, HarnessError> {
        Ok(fs::read(self.resolve(relative.as_ref())?)?)
    }

    /// Remove the whole temp tree now and report any failure.
    pub fn close(mut self) -> Result<(), HarnessError> {
        self.closed = true;
        fs::remove_dir_all(&self.path)?;
        Ok(())
    }

    /// Keep the directory for post-mortem inspection.
    pub fn keep(mut self) -> PathBuf {
        self.keep = true;
        self.path.clone()
    }

    fn resolve(&self, relative: &Path) -> Result<PathBuf, HarnessError> {
        if relative.is_absolute()
            || relative.components().any(|component| {
                matches!(
                    component,
                    Component::ParentDir | Component::RootDir | Component::Prefix(_)
                )
            })
        {
            return Err(HarnessError::InvalidRelativePath(relative.to_path_buf()));
        }
        Ok(self.path.join(relative))
    }
}

impl Drop for TempDataDir {
    fn drop(&mut self) {
        if !self.keep && !self.closed {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
