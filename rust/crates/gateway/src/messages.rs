//! Anthropic Messages forwarding (`POST /v1/messages`).
//!
//! Ports `chat2api.Handler._compat_proxy` for
//! `upstream_path == "/api/v1/messages"`: the accepted body is forwarded
//! unchanged to `POST /api/v1/messages` with the stored upstream bearer and the
//! client's `Anthropic-Version`/`Anthropic-Beta` headers, and the response is
//! relayed byte for byte. The local `/api/v1/messages` alias is added by S2a AC6.

use std::collections::HashMap;
use std::net::TcpStream;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::{
    line_text, ChatResult, ClientWatcher, ReaderSlot, PRELUDE_MAX_BYTES, SSE_CONTENT_TYPE,
};
use crate::error::GatewayError;
use crate::gate::Admission;
use crate::http::{write_chunk, write_response_head, Request, Response};
use crate::server::AppState;
use crate::upstream::{post_compat, NextItem, StreamReader, UpstreamBody, UpstreamResponse};

/// The frozen Anthropic Messages field whitelist.
///
/// `max_tokens` is required and must be a positive integer; `stream` is optional
/// but must be a bool when present. Unknown fields are refused, never ignored.
pub const MESSAGES_FIELDS: [&str; 13] = [
    "max_tokens",
    "messages",
    "metadata",
    "model",
    "stop_sequences",
    "stream",
    "system",
    "temperature",
    "thinking",
    "tool_choice",
    "tools",
    "top_k",
    "top_p",
];

/// Python `valid_messages_request`: shape-only admission for one shared body.
///
/// Python's `bool` is an `int` subclass, so `max_tokens: true` is explicitly
/// not a positive integer; `serde_json`'s `as_u64` already refuses bools,
/// floats, strings and negative numbers.
pub fn valid_messages_request(body: &Map<String, Value>) -> bool {
    if body
        .keys()
        .any(|key| !MESSAGES_FIELDS.contains(&key.as_str()))
    {
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
    if !body
        .get("max_tokens")
        .and_then(Value::as_u64)
        .is_some_and(|value| value >= 1)
    {
        return false;
    }
    body.get("stream").map_or(true, Value::is_boolean)
}

struct AdmissionGuard<'a> {
    state: &'a AppState,
    admission: Option<Admission>,
}

impl Drop for AdmissionGuard<'_> {
    fn drop(&mut self) {
        if let (Some(gate), Some(admission)) = (self.state.gate.as_deref(), self.admission.take()) {
            gate.unregister_inflight(&admission.key_id, &admission.ctx);
            gate.release(&admission.key_id);
        }
    }
}

struct MessagesContext<'a> {
    state: &'a AppState,
    deadline: Instant,
    upstream_body: Map<String, Value>,
    anthropic_headers: Vec<(String, String)>,
    origin: Option<String>,
}

/// Serve one Anthropic Messages request.
pub fn handle_messages(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
    admission: Option<Admission>,
) -> ChatResult {
    let _guard = AdmissionGuard { state, admission };
    let body = match read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    ) {
        Ok(Some(body)) => body,
        Ok(None) => {
            return ChatResult::Response(GatewayError::unsupported_messages_request().response())
        }
        Err(error) => return ChatResult::Response(error.response()),
    };
    if state.gate.is_some() && !valid_messages_request(&body) {
        return ChatResult::Response(GatewayError::unsupported_messages_request().response());
    }
    let client_stream = body.get("stream").is_some_and(truthy);
    let started_at = Instant::now();
    let context = MessagesContext {
        state,
        deadline: started_at + state.config.request_total,
        upstream_body: body,
        anthropic_headers: anthropic_headers(request),
        origin: request
            .headers
            .get("Origin")
            .filter(|origin| state.config.allowed_origins.contains(*origin))
            .map(str::to_string),
    };
    if client_stream {
        forward_stream(&context, stream)
    } else {
        forward_json(&context)
    }
}

fn forward_json(context: &MessagesContext<'_>) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= context.deadline {
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exceeded the request deadline",
                    "request_deadline_exceeded",
                )
                .response(),
            );
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exhausted the physical request budget",
                    "upstream_attempt_budget_exhausted",
                )
                .response(),
            );
        }
        physical_attempts += 1;
        let attempt_started = Instant::now();
        let mut response = match post_compat(
            &context.state.config,
            "/api/v1/messages",
            &serde_json::to_vec(&Value::Object(context.upstream_body.clone())).unwrap_or_default(),
            false,
            context.deadline,
            &context.anthropic_headers,
        ) {
            Ok(response) => response,
            Err(failure) => {
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if !sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        return ChatResult::Response(deadline_response());
                    }
                    continue;
                }
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .response(),
                );
            }
        };
        if response.status != 200 {
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= context.state.config.slow_retry_threshold;
            if retryable_status(response.status)
                && retry_available(context, fast_retries, slow_retries, physical_attempts, slow)
            {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let status = response.status;
                let delay = retry_delay(context, fast_retries, slow);
                drop(response);
                if !sleep_with_deadline(delay, context.deadline) {
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Upstream compatibility endpoint reached the request deadline",
                            "request_deadline_exceeded",
                        )
                        .with_detail(Value::String(format!("HTTP {status}")))
                        .response(),
                    );
                }
                continue;
            }
        }
        let status = response.status;
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .response(),
                );
            }
        };
        return ChatResult::Response(passthrough_response(status, &response, raw));
    }
}

fn forward_stream(context: &MessagesContext<'_>, stream: &mut TcpStream) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= context.deadline {
            return ChatResult::Response(deadline_response());
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exhausted the physical request budget",
                    "upstream_attempt_budget_exhausted",
                )
                .response(),
            );
        }
        let slot = match context.state.stream_readers.acquire_until(context.deadline) {
            Some(slot) => slot,
            None => {
                return ChatResult::Response(
                    GatewayError::new(
                        503,
                        "Too many concurrent streaming requests",
                        "too_many_concurrent_streams",
                    )
                    .with_detail(json!(format!(
                        "concurrent reader limit {} reached; retry later",
                        context.state.config.max_stream_readers
                    )))
                    .response(),
                )
            }
        };
        physical_attempts += 1;
        let attempt_started = Instant::now();
        let mut response = match post_compat(
            &context.state.config,
            "/api/v1/messages",
            &serde_json::to_vec(&Value::Object(context.upstream_body.clone())).unwrap_or_default(),
            true,
            context.deadline,
            &context.anthropic_headers,
        ) {
            Ok(response) => response,
            Err(failure) => {
                slot.release();
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if !sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        return ChatResult::Response(deadline_response());
                    }
                    continue;
                }
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .response(),
                );
            }
        };
        if response.status != 200 {
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= context.state.config.slow_retry_threshold;
            if retryable_status(response.status)
                && retry_available(context, fast_retries, slow_retries, physical_attempts, slow)
            {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let status = response.status;
                let delay = retry_delay(context, fast_retries, slow);
                drop(response);
                slot.release();
                if !sleep_with_deadline(delay, context.deadline) {
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Upstream compatibility endpoint reached the request deadline",
                            "request_deadline_exceeded",
                        )
                        .with_detail(Value::String(format!("HTTP {status}")))
                        .response(),
                    );
                }
                continue;
            }
            let status = response.status;
            let raw = match response.body.read_all() {
                Ok(raw) => raw,
                Err(failure) => {
                    slot.release();
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Failed to reach upstream compatibility endpoint",
                            failure.name(),
                        )
                        .response(),
                    );
                }
            };
            slot.release();
            return ChatResult::Response(passthrough_response(status, &response, raw));
        }
        let upstream_headers = response.headers.clone();
        let outcome = consume_stream(
            context,
            stream,
            response.body,
            slot,
            &upstream_headers,
            attempt_started,
        );
        if outcome.status == MessagesStreamStatus::ClientDisconnected {
            return ChatResult::Written { close: true };
        }
        if !outcome.committed && outcome.status.is_retryable_precommit() {
            let slow = outcome.elapsed >= context.state.config.slow_retry_threshold;
            if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                if !sleep_with_deadline(retry_delay(context, fast_retries, slow), context.deadline)
                {
                    return ChatResult::Response(deadline_response());
                }
                continue;
            }
        }
        if outcome.committed && outcome.status == MessagesStreamStatus::UpstreamReadError {
            write_stream_error(stream, &outcome);
        } else if !outcome.committed && !outcome.status.is_success() {
            return ChatResult::Response(stream_error_response(&outcome));
        }
        return ChatResult::Written { close: true };
    }
}

struct MessagesSseState {
    committed: bool,
    staged: Vec<u8>,
    event_lines: Vec<String>,
    saw_terminal: bool,
    status: MessagesStreamStatus,
    exception: Option<&'static str>,
}

impl MessagesSseState {
    fn new() -> Self {
        Self {
            committed: false,
            staged: Vec::new(),
            event_lines: Vec::new(),
            saw_terminal: false,
            status: MessagesStreamStatus::Ok,
            exception: None,
        }
    }
}

struct MessagesStreamOutcome {
    committed: bool,
    status: MessagesStreamStatus,
    exception: Option<&'static str>,
    elapsed: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MessagesStreamStatus {
    Ok,
    ClientDisconnected,
    UpstreamFirstByteTimeout,
    UpstreamReadError,
    UpstreamEofWithoutEvent,
    UpstreamStreamError,
}

impl MessagesStreamStatus {
    fn reason(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::ClientDisconnected => "client_disconnected",
            Self::UpstreamFirstByteTimeout => "upstream_first_byte_timeout",
            Self::UpstreamReadError => "upstream_read_error",
            Self::UpstreamEofWithoutEvent => "upstream_eof_without_event",
            Self::UpstreamStreamError => "upstream_stream_error",
        }
    }

    fn is_success(self) -> bool {
        self == Self::Ok
    }

    fn is_retryable_precommit(self) -> bool {
        matches!(
            self,
            Self::UpstreamFirstByteTimeout
                | Self::UpstreamReadError
                | Self::UpstreamEofWithoutEvent
        )
    }
}

enum EventAction {
    Continue,
    Stop(MessagesStreamStatus),
}

fn consume_stream(
    context: &MessagesContext<'_>,
    stream: &mut TcpStream,
    body: UpstreamBody,
    slot: Arc<ReaderSlot>,
    upstream_headers: &HashMap<String, String>,
    attempt_started: Instant,
) -> MessagesStreamOutcome {
    let watcher = ClientWatcher::start(stream);
    let reader = StreamReader::start(body, Some(slot));
    let mut state = MessagesSseState::new();
    let first_data_deadline =
        (Instant::now() + context.state.config.first_byte_timeout).min(context.deadline);
    loop {
        if watcher.as_ref().is_some_and(ClientWatcher::disconnected) {
            state.status = MessagesStreamStatus::ClientDisconnected;
            break;
        }
        if !state.committed && Instant::now() >= first_data_deadline {
            state.status = MessagesStreamStatus::UpstreamFirstByteTimeout;
            break;
        }
        match reader.next_item(Duration::from_millis(50)) {
            NextItem::Timeout => continue,
            NextItem::End => {
                if !state.event_lines.is_empty() {
                    if let EventAction::Stop(status) =
                        process_event(stream, context, &mut state, upstream_headers, false)
                    {
                        state.status = status;
                    }
                }
                if state.status == MessagesStreamStatus::Ok && !state.saw_terminal {
                    state.status = MessagesStreamStatus::UpstreamEofWithoutEvent;
                }
                break;
            }
            NextItem::Error(error) => {
                state.status = MessagesStreamStatus::UpstreamReadError;
                state.exception = Some(error.name);
                break;
            }
            NextItem::Line(line) => {
                let text = line_text(&line);
                if text.is_empty() {
                    let action = process_event(stream, context, &mut state, upstream_headers, true);
                    state.event_lines.clear();
                    if let EventAction::Stop(status) = action {
                        state.status = status;
                        break;
                    }
                } else {
                    state.event_lines.push(text);
                }
            }
        }
    }
    reader.cancel();
    reader.release_slot();
    drop(reader);
    MessagesStreamOutcome {
        committed: state.committed,
        status: state.status,
        exception: state.exception,
        elapsed: attempt_started.elapsed(),
    }
}

fn process_event(
    stream: &mut TcpStream,
    context: &MessagesContext<'_>,
    state: &mut MessagesSseState,
    upstream_headers: &HashMap<String, String>,
    had_separator: bool,
) -> EventAction {
    let merged = state.event_lines.join("\n");
    let wire = if state.event_lines.is_empty() {
        b"\n".to_vec()
    } else {
        let mut wire = merged.into_bytes();
        if had_separator {
            wire.extend_from_slice(b"\n\n");
        } else {
            wire.push(b'\n');
        }
        wire
    };
    let data_text = state
        .event_lines
        .iter()
        .filter_map(|line| line.strip_prefix("data:"))
        .map(str::trim_start)
        .collect::<Vec<_>>()
        .join("\n");
    let event = if !data_text.is_empty() && data_text != "[DONE]" {
        serde_json::from_str::<Value>(&data_text).ok()
    } else {
        None
    };

    let mut text_piece = None;
    let mut thinking_piece = None;
    let mut tool_action = false;
    let mut terminal = false;
    if let Some(event) = event.as_ref() {
        if let Some(delta) = event.get("delta").and_then(Value::as_object) {
            if let Some(text) = delta.get("text").and_then(Value::as_str) {
                text_piece = Some(text);
            }
            if let Some(thinking) = delta.get("thinking").and_then(Value::as_str) {
                thinking_piece = Some(thinking);
            }
            if delta.get("type").and_then(Value::as_str) == Some("input_json_delta")
                && delta.get("partial_json").is_some_and(truthy)
            {
                tool_action = true;
            }
        }
        if event
            .get("content_block")
            .and_then(Value::as_object)
            .and_then(|block| block.get("type"))
            .and_then(Value::as_str)
            == Some("tool_use")
        {
            tool_action = true;
        }
        let event_type = event.get("type").and_then(Value::as_str);
        terminal = matches!(event_type, Some("message_stop" | "error"));
        if terminal {
            state.saw_terminal = true;
            if event_type == Some("error") {
                state.status = MessagesStreamStatus::UpstreamStreamError;
            }
        }
    }

    if state.committed {
        if write_chunk(stream, &wire).is_err() {
            return EventAction::Stop(MessagesStreamStatus::ClientDisconnected);
        }
        return if terminal {
            EventAction::Stop(state.status)
        } else {
            EventAction::Continue
        };
    }
    state.staged.extend_from_slice(&wire);
    if state.staged.len() > PRELUDE_MAX_BYTES {
        if !flush_prelude(stream, context, state, upstream_headers) {
            return EventAction::Stop(MessagesStreamStatus::ClientDisconnected);
        }
        return if terminal {
            EventAction::Stop(state.status)
        } else {
            EventAction::Continue
        };
    }
    if thinking_piece.is_some_and(|piece| !piece.is_empty()) {
        if !flush_prelude(stream, context, state, upstream_headers) {
            return EventAction::Stop(MessagesStreamStatus::ClientDisconnected);
        }
        return if terminal {
            EventAction::Stop(state.status)
        } else {
            EventAction::Continue
        };
    }
    if text_piece.is_some_and(|piece| !piece.is_empty()) || tool_action || terminal {
        if !flush_prelude(stream, context, state, upstream_headers) {
            return EventAction::Stop(MessagesStreamStatus::ClientDisconnected);
        }
        return if terminal {
            EventAction::Stop(state.status)
        } else {
            EventAction::Continue
        };
    }
    EventAction::Continue
}

fn flush_prelude(
    stream: &mut TcpStream,
    context: &MessagesContext<'_>,
    state: &mut MessagesSseState,
    upstream_headers: &HashMap<String, String>,
) -> bool {
    state.committed = true;
    if !commit_headers(stream, context, upstream_headers) {
        return false;
    }
    let staged = std::mem::take(&mut state.staged);
    write_chunk(stream, &staged).is_ok()
}

fn commit_headers(
    stream: &mut TcpStream,
    context: &MessagesContext<'_>,
    upstream_headers: &HashMap<String, String>,
) -> bool {
    let mut headers = Vec::new();
    match upstream_headers.get("content-type") {
        Some(value) => headers.push(("Content-Type".to_string(), value.clone())),
        None => headers.push(("Content-Type".to_string(), SSE_CONTENT_TYPE.to_string())),
    }
    if let Some(value) = upstream_headers.get("cache-control") {
        headers.push(("Cache-Control".to_string(), value.clone()));
    }
    if let Some(origin) = &context.origin {
        headers.push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
    }
    headers.push(("Connection".to_string(), "close".to_string()));
    write_response_head(stream, 200, &headers).is_ok()
}

fn stream_error_response(outcome: &MessagesStreamOutcome) -> Response {
    GatewayError::new(
        502,
        "Upstream compatibility stream failed before any event",
        "upstream_stream_error",
    )
    .with_detail(Value::String(outcome.status.reason().to_string()))
    .response()
}

fn write_stream_error(stream: &mut TcpStream, outcome: &MessagesStreamOutcome) {
    let name = outcome.exception.unwrap_or("ConnectionError");
    let payload = json!({"type": "error", "error": {
        "type": "api_error",
        "message": format!(
            "Upstream connection failed mid-stream ({name}); the completion is incomplete."
        ),
    }});
    let mut event = b"event: error\ndata: ".to_vec();
    if let Ok(raw) = serde_json::to_vec(&payload) {
        event.extend_from_slice(&raw);
    }
    event.extend_from_slice(b"\n\n");
    let _ = write_chunk(stream, &event);
}

fn passthrough_response(status: u16, upstream: &UpstreamResponse, raw: Vec<u8>) -> Response {
    let mut response = Response::new(status, raw);
    for name in ["content-type", "content-length", "cache-control"] {
        if let Some(value) = upstream.headers.get(name) {
            response = response.with_header(name, value);
        }
    }
    response
}

fn anthropic_headers(request: &Request) -> Vec<(String, String)> {
    let mut headers = Vec::new();
    for name in ["Anthropic-Version", "Anthropic-Beta"] {
        if let Some(value) = request.headers.get(name).filter(|value| !value.is_empty()) {
            headers.push((name.to_string(), value.to_string()));
        }
    }
    headers
}

fn retry_available(
    context: &MessagesContext<'_>,
    fast_retries: u32,
    slow_retries: u32,
    physical_attempts: u32,
    slow: bool,
) -> bool {
    fast_retries < context.state.config.max_fast_retries
        && (!slow || slow_retries < context.state.config.max_slow_retries)
        && physical_attempts < context.state.config.max_physical_attempts
        && Instant::now() < context.deadline
}

fn retry_delay(context: &MessagesContext<'_>, retry_number: u32, slow: bool) -> Duration {
    if slow {
        return context.state.config.retry_delay_base * 2;
    }
    let exponent = retry_number.saturating_sub(1).min(4);
    (context.state.config.retry_delay_base * (1u32 << exponent)).min(Duration::from_secs(2))
}

fn sleep_with_deadline(delay: Duration, deadline: Instant) -> bool {
    if delay.is_zero() {
        return Instant::now() < deadline;
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return false;
    }
    thread::sleep(delay.min(remaining));
    Instant::now() < deadline
}

fn retryable_status(status: u16) -> bool {
    matches!(status, 408 | 429 | 500 | 502 | 503 | 504)
}

fn deadline_response() -> Response {
    GatewayError::new(
        502,
        "Upstream compatibility endpoint exceeded the request deadline",
        "request_deadline_exceeded",
    )
    .response()
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}
