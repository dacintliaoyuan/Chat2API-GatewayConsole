//! Anthropic Messages forwarding (`POST /v1/messages`).
//!
//! Ports `chat2api.Handler._compat_proxy` for
//! `upstream_path == "/api/v1/messages"`: the accepted body is forwarded
//! unchanged to `POST /api/v1/messages` with the stored upstream bearer and the
//! client's `Anthropic-Version`/`Anthropic-Beta` headers, and the response is
//! relayed byte for byte. The local `/api/v1/messages` alias is added by S2a AC6.

use std::net::TcpStream;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::ChatResult;
use crate::error::GatewayError;
use crate::gate::Admission;
use crate::http::{Request, Response};
use crate::server::AppState;
use crate::upstream::{post_compat, UpstreamResponse};

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
}

/// Serve one Anthropic Messages request.
pub fn handle_messages(
    state: &AppState,
    request: &Request,
    _stream: &mut TcpStream,
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
    if client_stream {
        return ChatResult::Response(
            GatewayError::not_implemented("messages_streaming_s2a").response(),
        );
    }
    let context = MessagesContext {
        state,
        deadline: Instant::now() + state.config.request_total,
        upstream_body: body,
        anthropic_headers: anthropic_headers(request),
    };
    forward_json(&context)
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
