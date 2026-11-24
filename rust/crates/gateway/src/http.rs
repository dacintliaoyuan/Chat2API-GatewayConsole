//! Minimal HTTP/1.1 transport for the S1a candidate.
//!
//! The parser deliberately keeps every header occurrence: duplicate
//! `Authorization`/`Content-Length`/`Content-Type` values are a security
//! boundary in the Python service, so a `BTreeMap<String, String>` would hide
//! them. The body reader only follows the first `Content-Length`, matching the
//! Python `email.message.Message.get` behavior; policies that reject duplicates
//! inspect [`Headers::get_all`] before using the body.

use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// Maximum header block accepted before the request is refused.
pub const MAX_HEADER_BYTES: usize = 64 * 1024;
/// Upper bound read into memory; a larger declared body is refused by policy.
pub const HARD_BODY_CAP: u64 = 1024 * 1024 * 1024;

/// Test for EOF without consuming any pipelined bytes.
pub(crate) fn peer_disconnected(stream: &TcpStream, restore_timeout: Duration) -> bool {
    if stream.set_read_timeout(Some(Duration::from_millis(1))).is_err() {
        return false;
    }
    let disconnected = match stream.peek(&mut [0; 1]) {
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
    let _ = stream.set_read_timeout(Some(restore_timeout));
    disconnected
}

pub(crate) fn accept_connections(
    shutdown: &AtomicBool,
    mut accept: impl FnMut() -> io::Result<(TcpStream, SocketAddr)>,
    mut start: impl FnMut(TcpStream, SocketAddr) -> io::Result<()>,
    mut report: impl FnMut(&'static str, &io::Error),
) {
    let mut last_accept_error: Option<Instant> = None;
    let mut last_spawn_error: Option<Instant> = None;
    while !shutdown.load(Ordering::SeqCst) {
        let (operation, error, last_error) = match accept() {
            Ok((stream, peer)) => match start(stream, peer) {
                Ok(()) => continue,
                Err(error) => ("thread_spawn", error, &mut last_spawn_error),
            },
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(2));
                continue;
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => ("accept", error, &mut last_accept_error),
        };
        if last_error.map_or(true, |at| at.elapsed() >= Duration::from_secs(5)) {
            *last_error = Some(Instant::now());
            report(operation, &error);
        }
        // Resource pressure can reject connections or thread creation while the
        // bound socket remains usable. Back off without dropping that socket.
        thread::sleep(Duration::from_millis(50));
    }
}

/// One raw header occurrence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub name: String,
    pub value: String,
}

/// Ordered header collection that preserves duplicates.
#[derive(Debug, Clone, Default)]
pub struct Headers {
    entries: Vec<Header>,
}

impl Headers {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, name: impl Into<String>, value: impl Into<String>) {
        self.entries.push(Header {
            name: name.into(),
            value: value.into(),
        });
    }

    pub fn get_all(&self, name: &str) -> Vec<&str> {
        self.entries
            .iter()
            .filter(|header| header.name.eq_ignore_ascii_case(name))
            .map(|header| header.value.as_str())
            .collect()
    }

    /// First value, like Python's `email.message.Message.get`.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.entries
            .iter()
            .find(|header| header.name.eq_ignore_ascii_case(name))
            .map(|header| header.value.as_str())
    }

    pub fn has(&self, name: &str) -> bool {
        self.get(name).is_some()
    }

    pub fn iter(&self) -> impl Iterator<Item = &Header> {
        self.entries.iter()
    }
}

/// The body state parsed from the request framing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BodyRead {
    Complete(Vec<u8>),
    InvalidLength,
    NegativeLength,
    TooLarge(u64),
    Incomplete,
}

/// One parsed HTTP request.
#[derive(Debug, Clone)]
pub struct Request {
    pub method: String,
    pub target: String,
    pub headers: Headers,
    pub body: BodyRead,
    pub peer_ip: std::net::IpAddr,
    /// The gateway request ID used for error response headers and in-band
    /// stream errors. It is generated once per request, not once per error.
    pub request_id: String,
}

impl Request {
    /// Path without the query string, matching `urllib.parse.urlparse().path`.
    pub fn path(&self) -> &str {
        self.target.split('?').next().unwrap_or(&self.target)
    }

    /// Raw target including the query string.
    pub fn raw_target(&self) -> &str {
        &self.target
    }
}

/// A response ready to be written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub close: bool,
}

impl Response {
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body: body.into(),
            close: false,
        }
    }

    pub fn json(status: u16, value: &Value) -> Self {
        let body = serde_json::to_vec(value).unwrap_or_else(|_| b"{}".to_vec());
        Self::new(status, body).with_header("Content-Type", "application/json; charset=utf-8")
    }

    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    pub fn closing(mut self) -> Self {
        self.close = true;
        self
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

/// A request that cannot be parsed at all.
#[derive(Debug)]
pub struct ReadError;

/// A parsed request head, before the body has been read.
///
/// The body limit depends on the route (share routes use the share limit, the
/// local `_read_body` routes use `MAX_REQUEST_BYTES`, and routes that never
/// read a body keep the hard cap), so the caller selects the limit after seeing
/// the method and target but before any body byte is read.
#[derive(Debug, Clone)]
pub struct RequestHead {
    pub method: String,
    pub target: String,
    pub headers: Headers,
    pub peer_ip: std::net::IpAddr,
    /// Generated as soon as the head parses so body-read errors can still
    /// report the same request ID as every other error on this request.
    pub request_id: String,
    leftover: Vec<u8>,
}

impl RequestHead {
    /// Path without the query string, matching `urllib.parse.urlparse().path`.
    pub fn path(&self) -> &str {
        self.target.split('?').next().unwrap_or(&self.target)
    }
}

/// Read one request head from `stream`; `Ok(None)` is a clean EOF between
/// requests. No body byte is consumed beyond what arrived with the head.
pub fn read_request_head(
    stream: &mut TcpStream,
    peer_ip: std::net::IpAddr,
) -> io::Result<Option<RequestHead>> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        if raw.len() > MAX_HEADER_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "request header block is too large",
            ));
        }
        let read = match stream.read(&mut buffer) {
            Ok(read) => read,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                // An idle keep-alive connection timed out waiting for the
                // next request head. That is a clean end of the connection,
                // not a malformed request: close without a synthetic 400.
                // A partial head that timed out is not clean: keep the error
                // path so the caller answers 400 for the truncated request.
                if raw.is_empty() {
                    return Ok(None);
                }
                return Err(error);
            }
            Err(error) => return Err(error),
        };
        if read == 0 {
            if raw.is_empty() {
                return Ok(None);
            }
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "request ended before the header block",
            ));
        }
        raw.extend_from_slice(&buffer[..read]);
    };

    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let target = parts.next().unwrap_or_default().to_string();
    if method.is_empty() || target.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid request line",
        ));
    }
    let mut headers = Headers::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push(name.trim(), value.trim());
        }
    }

    Ok(Some(RequestHead {
        method,
        target,
        headers,
        peer_ip,
        request_id: random_hex(16),
        leftover: raw[header_end..].to_vec(),
    }))
}

/// Read the body for an already parsed head, refusing a declared body above
/// `max_body` BEFORE reading it. The Python handlers check `Content-Length`
/// before they read the body, so a declared-too-large request must not wait for
/// bytes that may never arrive.
pub fn read_request_body(
    stream: &mut TcpStream,
    head: RequestHead,
    max_body: u64,
) -> io::Result<(Request, Vec<u8>)> {
    read_request_body_with_reserve(stream, head, max_body, reserve_body_bytes)
}

fn reserve_body_bytes(buffer: &mut Vec<u8>, additional: usize) -> io::Result<()> {
    buffer
        .try_reserve(additional)
        .map_err(|_| io::ErrorKind::OutOfMemory.into())
}

fn read_request_body_with_reserve(
    stream: &mut TcpStream,
    head: RequestHead,
    max_body: u64,
    mut reserve: impl FnMut(&mut Vec<u8>, usize) -> io::Result<()>,
) -> io::Result<(Request, Vec<u8>)> {
    let RequestHead {
        method,
        target,
        headers,
        peer_ip,
        request_id,
        mut leftover,
    } = head;
    let mut buffer = [0u8; 4096];
    let declared = headers.get("Content-Length").map(str::trim);
    let mut trailing = Vec::new();
    let body = match declared {
        None => {
            trailing = std::mem::take(&mut leftover);
            BodyRead::Complete(Vec::new())
        }
        Some(value) => match value.parse::<i64>() {
            Err(_) => BodyRead::InvalidLength,
            Ok(length) if length < 0 => BodyRead::NegativeLength,
            Ok(length) if length as u64 > HARD_BODY_CAP => BodyRead::TooLarge(length as u64),
            Ok(length) if length as u64 > max_body => BodyRead::TooLarge(length as u64),
            Ok(length) => {
                let length = length as usize;
                while leftover.len() < length {
                    let read = stream.read(&mut buffer)?;
                    if read == 0 {
                        return Ok((
                            Request {
                                method,
                                target,
                                headers,
                                body: BodyRead::Incomplete,
                                peer_ip,
                                request_id,
                            },
                            leftover,
                        ));
                    }
                    reserve(&mut leftover, read)?;
                    leftover.extend_from_slice(&buffer[..read]);
                }
                if leftover.len() > length {
                    let extra = &leftover[length..];
                    reserve(&mut trailing, extra.len())?;
                    trailing.extend_from_slice(extra);
                    leftover.truncate(length);
                }
                BodyRead::Complete(leftover)
            }
        },
    };

    Ok((
        Request {
            method,
            target,
            headers,
            body,
            peer_ip,
            request_id,
        },
        trailing,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Shutdown, TcpListener};

    use crate::{error::ErrorProtocol, server::write_memory_pressure};

    #[test]
    fn body_allocation_failure_is_a_request_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut stream, peer) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        // Parse the head before sending the body so the test must drive a growth.
        client
            .write_all(b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 4\r\n\r\n")
            .unwrap();
        let head = read_request_head(&mut stream, peer.ip()).unwrap().unwrap();
        let request_id = head.request_id.clone();
        client.write_all(b"test").unwrap();

        let error = read_request_body_with_reserve(&mut stream, head, 4, |buffer, _| {
            reserve_body_bytes(buffer, usize::MAX)
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
        write_memory_pressure(&mut stream, ErrorProtocol::OpenAi, &request_id).unwrap();
        stream.shutdown(Shutdown::Write).unwrap();

        let mut wire = String::new();
        client.read_to_string(&mut wire).unwrap();
        let (head, body) = wire.split_once("\r\n\r\n").unwrap();
        assert!(head.starts_with("HTTP/1.1 503"));
        assert!(head.contains(&format!("X-Request-Id: {request_id}\r\n")));
        assert!(head.contains(&format!("Content-Length: {}\r\n", body.len())));
        assert!(head.ends_with("Connection: close"));
        let body: Value = serde_json::from_str(body).unwrap();
        assert_eq!(body["error"]["code"], "gateway_memory_pressure");
    }

    #[test]
    fn unavailable_accept_and_thread_keep_listener_running() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let _first = TcpStream::connect(addr).unwrap();
        let _second = TcpStream::connect(addr).unwrap();
        let shutdown = AtomicBool::new(false);
        let mut accept_calls = 0;
        let mut start_calls = 0;
        let mut failures = Vec::new();

        accept_connections(
            &shutdown,
            || {
                accept_calls += 1;
                if accept_calls == 1 {
                    return Err(io::Error::from_raw_os_error(10055));
                }
                listener.accept()
            },
            |_, _| {
                start_calls += 1;
                if start_calls == 1 {
                    return Err(io::Error::from_raw_os_error(1455));
                }
                shutdown.store(true, Ordering::SeqCst);
                Ok(())
            },
            |operation, error| failures.push((operation, error.raw_os_error())),
        );

        assert_eq!(accept_calls, 3);
        assert_eq!(start_calls, 2);
        assert_eq!(
            failures,
            [("accept", Some(10055)), ("thread_spawn", Some(1455))]
        );
    }
}

/// Read one request from `stream`; `Ok(None)` is a clean EOF between requests.
pub fn read_request(
    stream: &mut TcpStream,
    peer_ip: std::net::IpAddr,
) -> io::Result<Option<Request>> {
    read_request_with_body_cap(stream, peer_ip, HARD_BODY_CAP)
}

/// Read one request, refusing a declared body above `max_body` BEFORE reading
/// it. The Python console checks `Content-Length` before it reads the body, so a
/// declared-too-large request must not wait for bytes that may never arrive.
pub fn read_request_with_body_cap(
    stream: &mut TcpStream,
    peer_ip: std::net::IpAddr,
    max_body: u64,
) -> io::Result<Option<Request>> {
    let Some(head) = read_request_head(stream, peer_ip)? else {
        return Ok(None);
    };
    read_request_body(stream, head, max_body).map(|(request, _)| Some(request))
}

/// Read one request and return the bytes that followed the declared body.
///
/// A WebSocket upgrade never has a body, so those bytes are the client's first
/// frames: dropping them would lose input that arrived in the same segment as the
/// handshake.
pub fn read_request_with_leftover(
    stream: &mut TcpStream,
    peer_ip: std::net::IpAddr,
    max_body: u64,
) -> io::Result<Option<(Request, Vec<u8>)>> {
    let Some(head) = read_request_head(stream, peer_ip)? else {
        return Ok(None);
    };
    read_request_body(stream, head, max_body).map(Some)
}

/// Write one response and report whether the connection must be closed.
pub fn write_response(stream: &mut TcpStream, response: &Response) -> io::Result<()> {
    let mut headers = response.headers.clone();
    let mut has_length = false;
    let mut has_connection = false;
    for (name, _) in &headers {
        if name.eq_ignore_ascii_case("content-length") {
            has_length = true;
        }
        if name.eq_ignore_ascii_case("connection") {
            has_connection = true;
        }
    }
    if !has_length {
        headers.push((
            "Content-Length".to_string(),
            response.body.len().to_string(),
        ));
    }
    if response.close && !has_connection {
        headers.push(("Connection".to_string(), "close".to_string()));
    }
    write_response_head(stream, response.status, &headers)?;
    stream.write_all(&response.body)?;
    stream.flush()
}

/// Write one status line and header block without a body or a length.
pub fn write_response_head(
    stream: &mut TcpStream,
    status: u16,
    headers: &[(String, String)],
) -> io::Result<()> {
    let mut head = format!("HTTP/1.1 {status} {}\r\n", reason_phrase(status));
    for (name, value) in headers {
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    stream.flush()
}

/// Write one already-framed body chunk and flush it downstream.
pub fn write_chunk(stream: &mut TcpStream, bytes: &[u8]) -> io::Result<()> {
    stream.write_all(bytes)?;
    stream.flush()
}

pub fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        308 => "Permanent Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        408 => "Request Timeout",
        409 => "Conflict",
        410 => "Gone",
        413 => "Payload Too Large",
        415 => "Unsupported Media Type",
        426 => "Upgrade Required",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "Unknown",
    }
}

/// Python `secrets.token_hex(bytes)`: lowercase hex with `2 * bytes` chars.
pub(crate) fn random_hex(bytes: usize) -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let mut hasher = Sha256::new();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    hasher.update(nanos.to_le_bytes());
    hasher.update(COUNTER.fetch_add(1, Ordering::SeqCst).to_le_bytes());
    hasher.update(std::process::id().to_le_bytes());
    let digest = hasher.finalize();
    let mut output = String::new();
    for byte in digest.iter().take(bytes) {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}
