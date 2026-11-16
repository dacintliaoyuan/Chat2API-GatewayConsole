//! Anthropic Messages forwarding (`POST /v1/messages`).
//!
//! Ports `chat2api.Handler._compat_proxy` for
//! `upstream_path == "/api/v1/messages"`: the accepted body is forwarded
//! unchanged to `POST /api/v1/messages` with the stored upstream bearer and the
//! client's `Anthropic-Version`/`Anthropic-Beta` headers, and the response is
//! relayed byte for byte. The local `/api/v1/messages` alias is added by S2a AC6.

use std::collections::HashMap;
use std::net::TcpStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::{line_text, ChatResult, ClientWatcher, PRELUDE_MAX_BYTES, SSE_CONTENT_TYPE};
use crate::error::GatewayError;
use crate::gate::Admission;
use crate::http::{write_chunk, write_response_head, Request, Response};
use crate::ledger::{
    anthropic_message_usage, anthropic_output_estimate, anthropic_usage_fields, merge_usage_max,
    message_tokens, usage_fields_to_json, NormalizedUsage, UsageRecord,
};
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
    model: &'a str,
    started_at: Instant,
    deadline: Instant,
    client_stream: bool,
    input_estimate: u64,
    key_id: Option<&'a str>,
    request_id: Option<&'a str>,
    attribution: &'a str,
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
    let key_id = admission.as_ref().map(|value| value.key_id.clone());
    let request_id = key_id.as_ref().map(|_| random_request_id());
    let attribution = if key_id.is_some() {
        "verified"
    } else {
        "absent"
    };
    let _guard = AdmissionGuard { state, admission };
    let body = match read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    ) {
        Ok(Some(body)) => body,
        Ok(None) => {
            return ChatResult::Response(
                GatewayError::unsupported_messages_request().anthropic_response(),
            )
        }
        Err(error) => return ChatResult::Response(error.anthropic_response()),
    };
    if state.gate.is_some() && !valid_messages_request(&body) {
        return ChatResult::Response(
            GatewayError::unsupported_messages_request().anthropic_response(),
        );
    }
    let client_stream = body.get("stream").is_some_and(truthy);
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let started_at = Instant::now();
    let context = MessagesContext {
        state,
        model: &model,
        started_at,
        deadline: started_at + state.config.request_total,
        client_stream,
        input_estimate: message_tokens(body.get("messages")),
        key_id: key_id.as_deref(),
        request_id: request_id.as_deref(),
        attribution,
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

/// Python `_compat_proxy`: 401 is a reauthentication failure, 400/422 keeps
/// the upstream status, everything else is `upstream_unavailable`.
fn compat_upstream_error(status: u16, text: &str) -> GatewayError {
    match status {
        401 => GatewayError::reauthentication_required(Some("upstream_rejected")),
        400 | 422 => GatewayError::upstream_rejected(status),
        _ => GatewayError::upstream_request_failed(status, text),
    }
}

fn compat_ledger_status(status: u16) -> String {
    match status {
        401 => "reauthentication_required".to_string(),
        400 | 422 => format!("HTTP{status}"),
        _ => "upstream_unavailable".to_string(),
    }
}

fn record_usage(
    context: &MessagesContext<'_>,
    status: &str,
    usage: Option<&Value>,
    output_estimate: u64,
) {
    context.state.ledger.record(UsageRecord {
        model: context.model,
        stream: context.client_stream,
        started_at: context.started_at,
        usage,
        input_estimate: context.input_estimate,
        output_estimate,
        status,
        protocol: Some("messages"),
        key_id: context.key_id,
        request_id: context.request_id,
        attribution: context.attribution,
    });
}

fn forward_json(context: &MessagesContext<'_>) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= context.deadline {
            record_usage(context, "request_deadline_exceeded", None, 0);
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exceeded the request deadline",
                    "request_deadline_exceeded",
                )
                .anthropic_response(),
            );
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            record_usage(context, "upstream_attempt_budget_exhausted", None, 0);
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exhausted the physical request budget",
                    "upstream_attempt_budget_exhausted",
                )
                .anthropic_response(),
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
                    if sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        continue;
                    }
                }
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .anthropic_response(),
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
                    record_usage(context, "request_deadline_exceeded", None, 0);
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Upstream compatibility endpoint reached the request deadline",
                            "request_deadline_exceeded",
                        )
                        .with_detail(Value::String(format!("HTTP {status}")))
                        .anthropic_response(),
                    );
                }
                continue;
            }
        }
        let status = response.status;
        if status != 200 {
            let text = response
                .body
                .read_prefix(2048)
                .ok()
                .map(|raw| String::from_utf8_lossy(&raw).to_string())
                .unwrap_or_default();
            let ledger_status = compat_ledger_status(status);
            record_usage(context, &ledger_status, None, 0);
            return ChatResult::Response(compat_upstream_error(status, &text).anthropic_response());
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .anthropic_response(),
                );
            }
        };
        let payload = serde_json::from_slice::<Value>(&raw).ok();
        let usage = payload.as_ref().and_then(anthropic_message_usage);
        let output_estimate = payload.as_ref().map(anthropic_output_estimate).unwrap_or(0);
        let usage_value = usage.as_ref().map(usage_fields_to_json);
        record_usage(context, "ok", usage_value.as_ref(), output_estimate);
        return ChatResult::Response(passthrough_response(status, &response, raw));
    }
}

fn forward_stream(context: &MessagesContext<'_>, stream: &mut TcpStream) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= context.deadline {
            record_usage(context, "request_deadline_exceeded", None, 0);
            return ChatResult::Response(deadline_response());
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            record_usage(context, "upstream_attempt_budget_exhausted", None, 0);
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exhausted the physical request budget",
                    "upstream_attempt_budget_exhausted",
                )
                .anthropic_response(),
            );
        }
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
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        continue;
                    }
                }
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(
                    GatewayError::new(
                        502,
                        "Failed to reach upstream compatibility endpoint",
                        failure.name(),
                    )
                    .anthropic_response(),
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
                    record_usage(context, "request_deadline_exceeded", None, 0);
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Upstream compatibility endpoint reached the request deadline",
                            "request_deadline_exceeded",
                        )
                        .with_detail(Value::String(format!("HTTP {status}")))
                        .anthropic_response(),
                    );
                }
                continue;
            }
            let status = response.status;
            let text = response
                .body
                .read_prefix(2048)
                .ok()
                .map(|raw| String::from_utf8_lossy(&raw).to_string())
                .unwrap_or_default();
            let ledger_status = compat_ledger_status(status);
            record_usage(context, &ledger_status, None, 0);
            return ChatResult::Response(compat_upstream_error(status, &text).anthropic_response());
        }
        let upstream_headers = response.headers.clone();
        let outcome = consume_stream(
            context,
            stream,
            response.body,
            &upstream_headers,
            attempt_started,
        );
        if outcome.status == MessagesStreamStatus::ClientDisconnected {
            record_stream_usage(context, &outcome);
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
                    record_usage(context, "upstream_stream_error", None, 0);
                    return ChatResult::Response(stream_error_response(&outcome));
                }
                continue;
            }
        }
        if outcome.committed && outcome.status == MessagesStreamStatus::UpstreamReadError {
            write_stream_error(stream, &outcome);
            record_stream_usage(context, &outcome);
        } else if !outcome.committed && !outcome.status.is_success() {
            record_usage(context, "upstream_stream_error", None, 0);
            return ChatResult::Response(stream_error_response(&outcome));
        } else {
            record_stream_usage(context, &outcome);
        }
        return ChatResult::Written { close: true };
    }
}

fn record_stream_usage(context: &MessagesContext<'_>, outcome: &MessagesStreamOutcome) {
    record_usage(
        context,
        outcome.status.reason(),
        outcome.usage.as_ref(),
        outcome.output_chars / 4,
    );
}

struct MessagesSseState {
    committed: bool,
    staged: Vec<u8>,
    event_lines: Vec<String>,
    saw_terminal: bool,
    status: MessagesStreamStatus,
    exception: Option<&'static str>,
    usage: AnthropicUsageAccumulator,
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
            usage: AnthropicUsageAccumulator::default(),
        }
    }
}

#[derive(Default)]
struct AnthropicUsageAccumulator {
    fields: NormalizedUsage,
    saw_input: bool,
    saw_output: bool,
    output_chars: u64,
}

impl AnthropicUsageAccumulator {
    fn observe(&mut self, event: &Value) {
        let event_type = event.get("type").and_then(Value::as_str);
        match event_type {
            Some("message_start") => {
                let usage = event
                    .get("message")
                    .and_then(Value::as_object)
                    .and_then(|message| message.get("usage"));
                let fields = anthropic_usage_fields(usage, true, false);
                self.saw_input = !fields.is_empty();
                self.merge(fields);
            }
            Some("message_delta") => {
                let fields = anthropic_usage_fields(event.get("usage"), false, true);
                if fields.completion_tokens.is_some() {
                    self.saw_output = true;
                }
                self.merge(fields);
            }
            _ => {}
        }
    }

    fn merge(&mut self, fields: NormalizedUsage) {
        self.fields = merge_usage_max(self.fields.clone(), fields);
    }

    fn observe_text(&mut self, piece: Option<&str>) {
        if let Some(piece) = piece.filter(|piece| !piece.is_empty()) {
            self.output_chars += piece.chars().count() as u64;
        }
    }

    fn usage(&self) -> Option<Value> {
        if self.fields.is_empty() {
            None
        } else {
            Some(usage_fields_to_json(&self.fields))
        }
    }
}

struct MessagesStreamOutcome {
    committed: bool,
    status: MessagesStreamStatus,
    usage: Option<Value>,
    output_chars: u64,
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
    upstream_headers: &HashMap<String, String>,
    attempt_started: Instant,
) -> MessagesStreamOutcome {
    let watcher = ClientWatcher::start(stream);
    let reader = StreamReader::start(body);
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
    drop(reader);
    MessagesStreamOutcome {
        committed: state.committed,
        status: state.status,
        usage: state.usage.usage(),
        output_chars: state.usage.output_chars,
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
        state.usage.observe(event);
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
    state.usage.observe_text(thinking_piece);
    state.usage.observe_text(text_piece);

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
    .anthropic_response()
}

fn write_stream_error(stream: &mut TcpStream, outcome: &MessagesStreamOutcome) {
    let name = outcome.exception.unwrap_or("ConnectionError");
    let payload = json!({"type": "error", "error": {
        "type": "api_error",
        "message": format!(
            "Upstream connection failed mid-stream ({name}); the completion is incomplete."
        ),
        "code": "upstream_stream_error",
        "detail": {
            "reason": outcome.status.reason(),
            "exception": name,
        },
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
    .anthropic_response()
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

/// Python `secrets.token_hex(16)`: 32 lowercase hex characters.
fn random_request_id() -> String {
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
    digest[..16]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
