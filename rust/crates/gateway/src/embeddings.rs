//! OpenAI-compatible embeddings passthrough (`POST /v1/embeddings`).
//!
//! Ports `chat2api.Handler._compat_proxy("/api/v1/embeddings")`: the accepted
//! body is forwarded unchanged to the upstream compatibility endpoint, the
//! upstream status/headers/body are relayed unchanged and no usage ledger row is
//! written. Embeddings are not a conversation protocol.

use std::net::TcpStream;
use std::time::Instant;

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::ChatResult;
use crate::error::ErrorProtocol;
use crate::event_log::{AttemptStats, FinalFailure};
use crate::http::{Request, Response};
use crate::retry::{self, LastAttempt, RetryLimits, RetryOutcome, RetryState};
use crate::server::AppState;
use crate::upstream::post_compat;

/// Serve one embeddings request.
pub fn handle_embeddings(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
) -> ChatResult {
    let mut stats = AttemptStats::default();
    let started_at = Instant::now();
    let result = handle_embeddings_inner(state, request, stream, &mut stats);
    let Some(final_status) = result.final_failure_status() else {
        return result;
    };
    state.event_log.final_failure(FinalFailure {
        request_id: &request.request_id,
        protocol: "embeddings",
        final_status,
        stats,
        started_at,
    });
    result
}

fn handle_embeddings_inner(
    state: &AppState,
    request: &Request,
    _stream: &mut TcpStream,
    stats: &mut AttemptStats,
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
    let mut retry_state = RetryState::default();
    let mut last = LastAttempt::default();
    let limits = RetryLimits::from_config(&state.config);
    loop {
        if Instant::now() >= deadline {
            return ChatResult::Response(last.deadline_error().response());
        }
        if retry_state.physical_attempts >= state.config.max_physical_attempts {
            return ChatResult::Response(last.error().response());
        }
        retry_state.record_attempt();
        stats.record_attempt();
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
                last.record_transport(failure);
                if let Some(plan) = retry_state.plan(
                    RetryOutcome::Transport,
                    &limits,
                    deadline,
                    attempt_started.elapsed(),
                ) {
                    if retry::sleep_with_deadline(plan.delay, deadline) {
                        continue;
                    }
                    return ChatResult::Response(last.deadline_error().response());
                }
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        let status = response.status;
        if status != 200 {
            let elapsed = attempt_started.elapsed();
            let retry_after = response.headers.get("retry-after").cloned();
            let mut text =
                retry::needs_body_for_classification(status).then(|| response.read_error_text());
            last.record_status(status, text.clone(), retry_after.clone());
            let plan = retry_state.plan(
                RetryOutcome::Status {
                    status,
                    headers: &response.headers,
                    body_text: text.as_deref(),
                },
                &limits,
                deadline,
                elapsed,
            );
            if let Some(plan) = plan {
                // #63 trigger point: `plan.reauthentication` starts the
                // shared re-authentication session instead of this probe.
                let _reauthentication = plan.reauthentication;
                drop(response);
                if !retry::sleep_with_deadline(plan.delay, deadline) {
                    return ChatResult::Response(last.deadline_error().response());
                }
                continue;
            }
            if text.is_none() && status != 401 {
                text = Some(response.read_error_text());
                last.record_status(status, text, retry_after);
            }
            return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                last.record_transport(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        let mut translated = Response::new(status, raw);
        for name in ["content-type", "content-length", "cache-control"] {
            if let Some(value) = response.headers.get(name) {
                translated = translated.with_header(name, value);
            }
        }
        return ChatResult::Response(translated);
    }
}
