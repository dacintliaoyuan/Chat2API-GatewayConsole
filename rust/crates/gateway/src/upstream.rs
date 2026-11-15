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

use crate::chat::ReaderSlot;
use crate::config::{parse_upstream_base_url, GatewayConfig};

/// Largest response head accepted from the upstream.
pub const MAX_UPSTREAM_HEADER_BYTES: usize = 64 * 1024;
/// Largest non-stream body read into memory.
pub const MAX_UPSTREAM_BODY_BYTES: usize = 64 * 1024 * 1024;
/// Largest not-yet-complete SSE line held by the producer.
pub const MAX_UPSTREAM_LINE_BYTES: usize = 1024 * 1024;
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
}

/// The decoded body of one upstream response.
pub struct UpstreamBody {
    stream: TcpStream,
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

/// Open one `POST /api/chat/completions` request.
pub fn post_chat(
    config: &GatewayConfig,
    body: &[u8],
    streaming: bool,
    deadline: Instant,
) -> Result<UpstreamResponse, UpstreamFailure> {
    let (host, port) = parse_upstream_base_url(&config.upstream_base_url)
        .map_err(|_| UpstreamFailure::ConnectionError)?;
    let address = format!("{host}:{port}")
        .to_socket_addrs()
        .map_err(|_| UpstreamFailure::ConnectionError)?
        .next()
        .ok_or(UpstreamFailure::ConnectionError)?;
    let remaining = remaining(deadline);
    if remaining.is_zero() {
        return Err(UpstreamFailure::Timeout);
    }
    let setup_timeout = remaining.min(UPSTREAM_SETUP_TIMEOUT);
    let mut upstream = TcpStream::connect_timeout(&address, setup_timeout).map_err(|error| {
        if error.kind() == io::ErrorKind::TimedOut {
            UpstreamFailure::Timeout
        } else {
            UpstreamFailure::ConnectionError
        }
    })?;
    let read_timeout = if streaming {
        remaining.min(config.socket_silence_timeout)
    } else {
        remaining.min(NONSTREAM_READ_TIMEOUT)
    };
    let _ = upstream.set_read_timeout(Some(read_timeout.max(Duration::from_millis(1))));
    let _ = upstream.set_write_timeout(Some(setup_timeout.max(Duration::from_millis(1))));
    let mut request = format!(
        "POST /api/chat/completions HTTP/1.1\r\nHost: {host}:{port}\r\n\
             Authorization: Bearer {}\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n\r\n",
        config.upstream_token,
        body.len()
    )
    .into_bytes();
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

fn read_head(stream: &mut TcpStream) -> Result<ResponseHead, UpstreamFailure> {
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
    slot: Option<Arc<ReaderSlot>>,
}

impl StreamReader {
    /// Start the producer. The producer owns `body` and the reader permit.
    pub fn start(body: UpstreamBody, slot: Option<Arc<ReaderSlot>>) -> Self {
        let (sender, receiver) = sync_channel(UPSTREAM_QUEUE_MAX);
        let finished = Arc::new(AtomicBool::new(false));
        let error = Arc::new(Mutex::new(None));
        let cancel = Arc::new(AtomicBool::new(false));
        let worker_finished = Arc::clone(&finished);
        let worker_error = Arc::clone(&error);
        let worker_cancel = Arc::clone(&cancel);
        let worker_slot = slot.clone();
        thread::spawn(move || {
            produce(
                body,
                sender,
                worker_finished,
                worker_error,
                worker_cancel,
                worker_slot,
            );
        });
        Self {
            receiver,
            finished,
            error,
            cancel,
            slot,
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

    /// Release the reader slot before a retry; the producer's later release is
    /// idempotent.
    pub fn release_slot(&self) {
        if let Some(slot) = &self.slot {
            slot.release();
        }
    }

    /// Wait for the producer to close the upstream and release its permit.
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
    slot: Option<Arc<ReaderSlot>>,
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
    if let Some(slot) = slot {
        slot.release();
    }
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
