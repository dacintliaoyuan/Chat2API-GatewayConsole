//! Upstream HTTP client for the shared Chat forwarding path.
//!
//! The client speaks the same loopback HTTP/1.1 shape as Python
//! `chat2api.Proxy.call` (`POST /api/chat/completions` with the stored
//! bearer), decodes `Content-Length`, chunked and close-delimited bodies, and
//! exposes a cancellable SSE line producer. The producer reads the upstream one
//! byte at a time, checks cancellation on every byte, closes the upstream socket
//! in its own `finally`, and releases its reader slot exactly once.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::config::{
    parse_upstream_base_url, upstream_host_header, upstream_uses_tls, GatewayConfig,
};
use crate::tls::{self, UpstreamStream};

/// Largest response head accepted from the upstream.
pub const MAX_UPSTREAM_HEADER_BYTES: usize = 64 * 1024;
/// Largest non-stream body read into memory.
pub const MAX_UPSTREAM_BODY_BYTES: usize = 64 * 1024 * 1024;
/// Largest not-yet-complete SSE line held by the producer.
pub const MAX_UPSTREAM_LINE_BYTES: usize = 1024 * 1024;
/// Largest upstream error-body prefix used for classification and diagnostics.
pub const MAX_UPSTREAM_ERROR_TEXT: usize = 2048;
/// Backpressure bound of the SSE producer/consumer queue.
pub const UPSTREAM_QUEUE_MAX: usize = 256;
/// Connect/write cap of one upstream request, like Python's 15 second setup cap.
pub const UPSTREAM_SETUP_TIMEOUT: Duration = Duration::from_secs(15);
/// Non-stream body read cap, like Python's 600 second non-stream read cap.
pub const NONSTREAM_READ_TIMEOUT: Duration = Duration::from_secs(600);

/// Why an upstream request could not produce a usable response.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpstreamFailure {
    Timeout,
    ConnectionError,
    Protocol,
}

impl UpstreamFailure {
    pub fn name(self) -> &'static str {
        match self {
            Self::Timeout => "Timeout",
            Self::ConnectionError => "ConnectionError",
            Self::Protocol => "ProtocolError",
        }
    }
}

/// One upstream HTTP response with its body stream.
pub struct UpstreamResponse {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: UpstreamBody,
    error_text: Option<Result<String, UpstreamFailure>>,
}

impl UpstreamResponse {
    /// Read the bounded error-body prefix used for retry classification and
    /// `detail.upstream_message`. A read failure yields an empty string, exactly
    /// like the pre-#61 final-error path.
    pub fn read_error_text(&mut self) -> String {
        self.error_text
            .take()
            .unwrap_or_else(|| self.read_error_prefix())
            .unwrap_or_default()
    }

    /// Capture the same error text the retry classifier needs without another read.
    pub(crate) fn capture_error_text(&mut self) -> Result<&str, UpstreamFailure> {
        let text = self.read_error_prefix();
        self.error_text.insert(text).as_deref().map_err(|error| *error)
    }

    /// The compatibility branch previously discarded this response unread.
    /// Read only immediately available bytes so diagnostics cannot wait on it.
    pub(crate) fn read_discarded_error_text(&mut self) -> Result<String, UpstreamFailure> {
        self.body
            .stream
            .set_nonblocking(true)
            .map_err(|_| UpstreamFailure::ConnectionError)?;
        self.read_error_prefix()
    }

    fn read_error_prefix(&mut self) -> Result<String, UpstreamFailure> {
        self.body
            .read_prefix(MAX_UPSTREAM_ERROR_TEXT)
            .map(|raw| String::from_utf8_lossy(&raw).into_owned())
    }
}

/// The decoded body of one upstream response.
pub struct UpstreamBody {
    stream: UpstreamStream,
    buffered: Vec<u8>,
    buffered_index: usize,
    framing: Framing,
}

enum Framing {
    Identity { remaining: Option<u64> },
    Chunked(ChunkState),
}

enum ChunkState {
    Size,
    Data(u64),
    DataCrlf,
    Trailer,
}

enum BodyStep {
    Byte(u8),
    Eof,
    Continue,
    Error(UpstreamFailure),
}

impl UpstreamBody {
    /// Read one decoded body byte; `Ok(None)` is a clean EOF.
    pub fn read_byte(&mut self) -> Result<Option<u8>, UpstreamFailure> {
        if self.buffered_index < self.buffered.len() {
            let byte = self.buffered[self.buffered_index];
            self.buffered_index += 1;
            return Ok(Some(byte));
        }
        self.buffered.clear();
        self.buffered_index = 0;
        loop {
            let mut framing =
                std::mem::replace(&mut self.framing, Framing::Identity { remaining: None });
            let step = match &mut framing {
                Framing::Identity { remaining } => {
                    if *remaining == Some(0) {
                        BodyStep::Eof
                    } else {
                        match self.read_raw_byte()? {
                            Some(byte) => {
                                if let Some(value) = remaining {
                                    *value -= 1;
                                }
                                BodyStep::Byte(byte)
                            }
                            None => BodyStep::Eof,
                        }
                    }
                }
                Framing::Chunked(state) => match state {
                    ChunkState::Size => match self.read_raw_line()? {
                        None => BodyStep::Eof,
                        Some(line) => {
                            let size_text =
                                line.split(|byte| *byte == b';').next().unwrap_or(&line);
                            let size_text = std::str::from_utf8(size_text)
                                .map_err(|_| UpstreamFailure::Protocol)?
                                .trim();
                            let size = u64::from_str_radix(size_text, 16)
                                .map_err(|_| UpstreamFailure::Protocol)?;
                            if size == 0 {
                                *state = ChunkState::Trailer;
                            } else {
                                *state = ChunkState::Data(size);
                            }
                            BodyStep::Continue
                        }
                    },
                    ChunkState::Data(remaining) => {
                        if *remaining == 0 {
                            *state = ChunkState::DataCrlf;
                            BodyStep::Continue
                        } else {
                            match self.read_raw_byte()? {
                                Some(byte) => {
                                    *remaining -= 1;
                                    BodyStep::Byte(byte)
                                }
                                None => BodyStep::Error(UpstreamFailure::Protocol),
                            }
                        }
                    }
                    ChunkState::DataCrlf => {
                        let Some(carriage) = self.read_raw_byte()? else {
                            return Err(UpstreamFailure::Protocol);
                        };
                        let Some(newline) = self.read_raw_byte()? else {
                            return Err(UpstreamFailure::Protocol);
                        };
                        if carriage != b'\r' || newline != b'\n' {
                            return Err(UpstreamFailure::Protocol);
                        }
                        *state = ChunkState::Size;
                        BodyStep::Continue
                    }
                    ChunkState::Trailer => match self.read_raw_line()? {
                        None => BodyStep::Eof,
                        Some(line) if line.is_empty() => BodyStep::Eof,
                        Some(_) => BodyStep::Continue,
                    },
                },
            };
            self.framing = framing;
            match step {
                BodyStep::Byte(byte) => return Ok(Some(byte)),
                BodyStep::Eof => return Ok(None),
                BodyStep::Continue => continue,
                BodyStep::Error(error) => return Err(error),
            }
        }
    }

    /// Read a complete decoded body, bounded by the memory cap.
    pub fn read_all(&mut self) -> Result<Vec<u8>, UpstreamFailure> {
        let mut output = Vec::new();
        while let Some(byte) = self.read_byte()? {
            output.push(byte);
            if output.len() > MAX_UPSTREAM_BODY_BYTES {
                return Err(UpstreamFailure::Protocol);
            }
        }
        Ok(output)
    }

    /// Read at most `limit` decoded body bytes for an error detail.
    pub fn read_prefix(&mut self, limit: usize) -> Result<Vec<u8>, UpstreamFailure> {
        let mut output = Vec::new();
        while output.len() < limit {
            match self.read_byte()? {
                Some(byte) => output.push(byte),
                None => break,
            }
        }
        Ok(output)
    }

    fn read_raw_byte(&mut self) -> Result<Option<u8>, UpstreamFailure> {
        let mut byte = [0u8; 1];
        loop {
            match self.stream.read(&mut byte) {
                Ok(0) => return Ok(None),
                Ok(_) => return Ok(Some(byte[0])),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                // The upstream closes without a TLS close_notify after a
                // complete response (envoy does); rustls reports that as
                // UnexpectedEof. Python's requests treats it as a clean EOF,
                // so the body is complete, never a connection failure.
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
                Err(error)
                    if error.kind() == io::ErrorKind::TimedOut
                        || error.kind() == io::ErrorKind::WouldBlock =>
                {
                    return Err(UpstreamFailure::Timeout)
                }
                Err(_) => return Err(UpstreamFailure::ConnectionError),
            }
        }
    }

    fn read_raw_line(&mut self) -> Result<Option<Vec<u8>>, UpstreamFailure> {
        let mut line = Vec::new();
        loop {
            let Some(byte) = self.read_raw_byte()? else {
                return Ok(if line.is_empty() { None } else { Some(line) });
            };
            if byte == b'\n' {
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(Some(line));
            }
            line.push(byte);
            if line.len() > MAX_UPSTREAM_HEADER_BYTES {
                return Err(UpstreamFailure::Protocol);
            }
        }
    }
}

/// Connect to the first reachable resolved address.
///
/// A dual-stack name may list an IPv6 address first while this host has no
/// IPv6 route; like Python's requests, every resolved address is tried instead
/// of failing on the first one.
fn connect_first(
    addresses: Vec<std::net::SocketAddr>,
    timeout: Duration,
) -> Result<TcpStream, UpstreamFailure> {
    let mut last = UpstreamFailure::ConnectionError;
    for address in addresses {
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(stream) => return Ok(stream),
            Err(error) if error.kind() == io::ErrorKind::TimedOut => {
                last = UpstreamFailure::Timeout
            }
            Err(_) => {}
        }
    }
    Err(last)
}

/// Open one `POST /api/chat/completions` request.
pub fn post_chat(
    config: &GatewayConfig,
    bearer: &str,
    body: &[u8],
    streaming: bool,
    deadline: Instant,
) -> Result<UpstreamResponse, UpstreamFailure> {
    post_compat(
        config,
        bearer,
        "/api/chat/completions",
        body,
        streaming,
        deadline,
        &[],
    )
}

/// Open one compatibility `POST` request with the stored upstream bearer.
///
/// Python `Proxy.call` forwards the selected compatibility path and the optional
/// Anthropic headers; the client's local credential never leaves this process.
pub fn post_compat(
    config: &GatewayConfig,
    bearer: &str,
    path: &str,
    body: &[u8],
    streaming: bool,
    deadline: Instant,
    extra_headers: &[(String, String)],
) -> Result<UpstreamResponse, UpstreamFailure> {
    let (host, port) = parse_upstream_base_url(&config.upstream_base_url)
        .map_err(|_| UpstreamFailure::ConnectionError)?;
    let host_header = upstream_host_header(&config.upstream_base_url)
        .map_err(|_| UpstreamFailure::ConnectionError)?;
    let addresses: Vec<std::net::SocketAddr> = format!("{host}:{port}")
        .to_socket_addrs()
        .map_err(|_| UpstreamFailure::ConnectionError)?
        .collect();
    if addresses.is_empty() {
        return Err(UpstreamFailure::ConnectionError);
    }
    let remaining = remaining(deadline);
    if remaining.is_zero() {
        return Err(UpstreamFailure::Timeout);
    }
    let setup_timeout = remaining.min(UPSTREAM_SETUP_TIMEOUT);
    let upstream = connect_first(addresses, setup_timeout)?;
    let read_timeout = if streaming {
        remaining.min(config.socket_silence_timeout)
    } else {
        remaining.min(NONSTREAM_READ_TIMEOUT)
    };
    let _ = upstream.set_read_timeout(Some(read_timeout.max(Duration::from_millis(1))));
    let _ = upstream.set_write_timeout(Some(setup_timeout.max(Duration::from_millis(1))));
    let mut upstream: UpstreamStream = if upstream_uses_tls(&config.upstream_base_url) {
        Box::new(tls::wrap(upstream, &host).map_err(|_| UpstreamFailure::ConnectionError)?)
    } else {
        Box::new(upstream)
    };
    let mut request = format!(
        "POST {path} HTTP/1.1\r\nHost: {host_header}\r\n\
             Authorization: Bearer {}\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n",
        bearer,
        body.len()
    )
    .into_bytes();
    for (name, value) in extra_headers {
        request.extend_from_slice(name.as_bytes());
        request.extend_from_slice(b": ");
        request.extend_from_slice(value.as_bytes());
        request.extend_from_slice(b"\r\n");
    }
    request.extend_from_slice(b"\r\n");
    request.extend_from_slice(body);
    upstream
        .write_all(&request)
        .map_err(|_| UpstreamFailure::ConnectionError)?;
    upstream
        .flush()
        .map_err(|_| UpstreamFailure::ConnectionError)?;
    let (status, headers, leftover) = read_head(&mut upstream)?;
    let framing = framing_from_headers(&headers);
    Ok(UpstreamResponse {
        status,
        headers,
        error_text: None,
        body: UpstreamBody {
            stream: upstream,
            buffered: leftover,
            buffered_index: 0,
            framing,
        },
    })
}

fn framing_from_headers(headers: &HashMap<String, String>) -> Framing {
    if headers
        .get("transfer-encoding")
        .is_some_and(|value| value.to_ascii_lowercase().contains("chunked"))
    {
        return Framing::Chunked(ChunkState::Size);
    }
    let remaining = headers
        .get("content-length")
        .and_then(|value| value.trim().parse::<u64>().ok());
    Framing::Identity { remaining }
}

type ResponseHead = (u16, HashMap<String, String>, Vec<u8>);

fn read_head(stream: &mut dyn Read) -> Result<ResponseHead, UpstreamFailure> {
    let mut raw = Vec::new();
    let mut byte = [0u8; 1];
    let header_end = loop {
        if raw.ends_with(b"\r\n\r\n") {
            break raw.len();
        }
        if raw.len() > MAX_UPSTREAM_HEADER_BYTES {
            return Err(UpstreamFailure::Protocol);
        }
        match stream.read(&mut byte) {
            Ok(0) => return Err(UpstreamFailure::Protocol),
            Ok(_) => raw.push(byte[0]),
            Err(error)
                if error.kind() == io::ErrorKind::TimedOut
                    || error.kind() == io::ErrorKind::WouldBlock =>
            {
                return Err(UpstreamFailure::Timeout)
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(UpstreamFailure::ConnectionError),
        }
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or(UpstreamFailure::Protocol)?;
    let mut headers = HashMap::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers
                .entry(name.trim().to_ascii_lowercase())
                .or_insert_with(|| value.trim().to_string());
        }
    }
    Ok((status, headers, raw[header_end..].to_vec()))
}

/// One item yielded by the cancellable SSE producer.
#[derive(Debug)]
pub enum StreamItem {
    Line(Vec<u8>),
    End,
    Error(StreamError),
}

/// A producer-side stream failure.
#[derive(Debug, Clone)]
pub struct StreamError {
    pub name: &'static str,
    pub timed_out: bool,
}

/// The result of polling the producer.
#[derive(Debug)]
pub enum NextItem {
    Line(Vec<u8>),
    End,
    Timeout,
    Error(StreamError),
}

/// Cancellable line producer over one upstream SSE body.
pub struct StreamReader {
    receiver: Receiver<StreamItem>,
    finished: Arc<AtomicBool>,
    error: Arc<Mutex<Option<StreamError>>>,
    cancel: Arc<AtomicBool>,
}

impl StreamReader {
    /// Start the producer; it owns `body` and closes it when it exits.
    pub fn start(body: UpstreamBody) -> Self {
        let (sender, receiver) = sync_channel(UPSTREAM_QUEUE_MAX);
        let finished = Arc::new(AtomicBool::new(false));
        let error = Arc::new(Mutex::new(None));
        let cancel = Arc::new(AtomicBool::new(false));
        let worker_finished = Arc::clone(&finished);
        let worker_error = Arc::clone(&error);
        let worker_cancel = Arc::clone(&cancel);
        thread::spawn(move || {
            produce(body, sender, worker_finished, worker_error, worker_cancel);
        });
        Self {
            receiver,
            finished,
            error,
            cancel,
        }
    }

    /// Poll for the next item; `Timeout` means the producer is still alive.
    pub fn next_item(&self, timeout: Duration) -> NextItem {
        let deadline = Instant::now() + timeout;
        loop {
            let now = Instant::now();
            if now >= deadline {
                return NextItem::Timeout;
            }
            let slice = (deadline - now).min(Duration::from_millis(50));
            match self.receiver.recv_timeout(slice) {
                Ok(StreamItem::Line(line)) => return NextItem::Line(line),
                Ok(StreamItem::End) => return NextItem::End,
                Ok(StreamItem::Error(error)) => return NextItem::Error(error),
                Err(RecvTimeoutError::Timeout) => {
                    if self.finished.load(Ordering::SeqCst) {
                        return self.finish();
                    }
                }
                Err(RecvTimeoutError::Disconnected) => return self.finish(),
            }
        }
    }

    fn finish(&self) -> NextItem {
        if let Some(error) = self.error.lock().expect("stream error poisoned").clone() {
            NextItem::Error(error)
        } else {
            NextItem::End
        }
    }

    /// Stop at the next byte; the producer closes the upstream socket itself.
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::SeqCst);
    }

    /// Wait for the producer to close the upstream socket.
    pub fn wait_finished(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.finished.load(Ordering::SeqCst) {
                return true;
            }
            thread::sleep(Duration::from_millis(5));
        }
        self.finished.load(Ordering::SeqCst)
    }
}

impl Drop for StreamReader {
    fn drop(&mut self) {
        self.cancel();
    }
}

fn produce(
    mut body: UpstreamBody,
    sender: SyncSender<StreamItem>,
    finished: Arc<AtomicBool>,
    error: Arc<Mutex<Option<StreamError>>>,
    cancel: Arc<AtomicBool>,
) {
    let mut pending = Vec::new();
    loop {
        if cancel.load(Ordering::SeqCst) {
            break;
        }
        match body.read_byte() {
            Ok(None) => {
                if !pending.is_empty() {
                    let _ = sender.send(StreamItem::Line(std::mem::take(&mut pending)));
                }
                break;
            }
            Ok(Some(byte)) => {
                pending.push(byte);
                if pending.len() > MAX_UPSTREAM_LINE_BYTES {
                    *error.lock().expect("stream error poisoned") = Some(StreamError {
                        name: "ValueError",
                        timed_out: false,
                    });
                    break;
                }
                if byte == b'\n' {
                    let line = std::mem::take(&mut pending);
                    if sender.send(StreamItem::Line(line)).is_err() {
                        break;
                    }
                }
            }
            Err(failure) => {
                *error.lock().expect("stream error poisoned") = Some(StreamError {
                    name: failure.name(),
                    timed_out: failure == UpstreamFailure::Timeout,
                });
                break;
            }
        }
    }
    finished.store(true, Ordering::SeqCst);
    drop(body);
}

fn remaining(deadline: Instant) -> Duration {
    deadline.saturating_duration_since(Instant::now())
}

/// Read one already-decoded upstream response body, bounded by the memory cap.
pub fn read_body(mut body: UpstreamBody) -> Result<Vec<u8>, UpstreamFailure> {
    body.read_all()
}

/// Parse one JSON value from an upstream body.
pub fn parse_json(body: &[u8]) -> Result<Value, serde_json::Error> {
    serde_json::from_slice(body)
}
