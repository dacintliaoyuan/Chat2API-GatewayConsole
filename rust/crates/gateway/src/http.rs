//! Minimal HTTP/1.1 transport for the S1a candidate.
//!
//! The parser deliberately keeps every header occurrence: duplicate
//! `Authorization`/`Content-Length`/`Content-Type` values are a security
//! boundary in the Python service, so a `BTreeMap<String, String>` would hide
//! them. The body reader only follows the first `Content-Length`, matching the
//! Python `email.message.Message.get` behavior; policies that reject duplicates
//! inspect [`Headers::get_all`] before using the body.

use std::io::{self, Read, Write};
use std::net::TcpStream;

use serde_json::Value;

/// Maximum header block accepted before the request is refused.
pub const MAX_HEADER_BYTES: usize = 64 * 1024;
/// Upper bound read into memory; a larger declared body is refused by policy.
pub const HARD_BODY_CAP: u64 = 1024 * 1024 * 1024;

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
    Ok(read_request_with_leftover(stream, peer_ip, max_body)?.map(|(request, _)| request))
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
        let read = stream.read(&mut buffer)?;
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

    let declared = headers
        .get("Content-Length")
        .map(|value| value.trim().to_string());
    let mut leftover = raw[header_end..].to_vec();
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
                        return Ok(Some((
                            Request {
                                method,
                                target,
                                headers,
                                body: BodyRead::Incomplete,
                                peer_ip,
                            },
                            leftover,
                        )));
                    }
                    leftover.extend_from_slice(&buffer[..read]);
                }
                trailing = leftover.split_off(length);
                BodyRead::Complete(leftover)
            }
        },
    };

    Ok(Some((
        Request {
            method,
            target,
            headers,
            body,
            peer_ip,
        },
        trailing,
    )))
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
