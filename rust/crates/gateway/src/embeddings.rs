//! OpenAI-compatible embeddings passthrough (`POST /v1/embeddings`).
//!
//! Ports `chat2api.Handler._compat_proxy("/api/v1/embeddings")`: the accepted
//! body is forwarded unchanged to the upstream compatibility endpoint, the
//! upstream status/headers/body are relayed unchanged and no usage ledger row is
//! written. Embeddings are not a conversation protocol.

use std::net::TcpStream;
use std::thread;
use std::time::{Duration, Instant};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::ChatResult;
use crate::error::GatewayError;
use crate::http::{Request, Response};
use crate::server::AppState;
use crate::upstream::post_compat;

/// Serve one embeddings request.
pub fn handle_embeddings(
    state: &AppState,
    request: &Request,
    _stream: &mut TcpStream,
) -> ChatResult {
    let body = match read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    ) {
        Ok(Some(body)) => body,
        Ok(None) => serde_json::Map::new(),
        Err(error) => return ChatResult::Response(error.response()),
    };
    let raw = serde_json::to_vec(&serde_json::Value::Object(body)).unwrap_or_default();
    let deadline = Instant::now() + state.config.request_total;
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= deadline {
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream compatibility endpoint exceeded the request deadline",
                    "request_deadline_exceeded",
                )
                .response(),
            );
        }
        if physical_attempts >= state.config.max_physical_attempts {
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
            &state.config,
            "/api/v1/embeddings",
            &raw,
            false,
            deadline,
            &[],
        ) {
            Ok(response) => response,
            Err(failure) => {
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= state.config.slow_retry_threshold;
                if retry_available(
                    state,
                    fast_retries,
                    slow_retries,
                    physical_attempts,
                    slow,
                    deadline,
                ) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if sleep_with_deadline(retry_delay(state, fast_retries, slow), deadline) {
                        continue;
                    }
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
        if retryable_status(response.status) {
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= state.config.slow_retry_threshold;
            if retry_available(
                state,
                fast_retries,
                slow_retries,
                physical_attempts,
                slow,
                deadline,
            ) {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let status = response.status;
                let delay = retry_delay(state, fast_retries, slow);
                drop(response);
                if !sleep_with_deadline(delay, deadline) {
                    return ChatResult::Response(
                        GatewayError::new(
                            502,
                            "Upstream compatibility endpoint reached the request deadline",
                            "request_deadline_exceeded",
                        )
                        .with_detail(serde_json::json!(format!("HTTP {status}")))
                        .response(),
                    );
                }
                continue;
            }
        }
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
                )
            }
        };
        let mut translated = Response::new(response.status, raw);
        for name in ["content-type", "content-length", "cache-control"] {
            if let Some(value) = response.headers.get(name) {
                translated = translated.with_header(name, value);
            }
        }
        return ChatResult::Response(translated);
    }
}

fn retry_available(
    state: &AppState,
    fast_retries: u32,
    slow_retries: u32,
    physical_attempts: u32,
    slow: bool,
    deadline: Instant,
) -> bool {
    fast_retries < state.config.max_fast_retries
        && (!slow || slow_retries < state.config.max_slow_retries)
        && physical_attempts < state.config.max_physical_attempts
        && Instant::now() < deadline
}

fn retry_delay(state: &AppState, retry_number: u32, slow: bool) -> Duration {
    if slow {
        return state.config.retry_delay_base * 2;
    }
    let exponent = retry_number.saturating_sub(1).min(4);
    (state.config.retry_delay_base * (1u32 << exponent)).min(Duration::from_secs(2))
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
