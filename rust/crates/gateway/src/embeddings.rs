//! OpenAI-compatible embeddings passthrough (`POST /v1/embeddings`).
//!
//! Ports `chat2api.Handler._compat_proxy("/api/v1/embeddings")`: the accepted
//! body is forwarded unchanged to the upstream compatibility endpoint, the
//! upstream status/headers/body are relayed unchanged and no usage ledger row is
//! written. Embeddings are not a conversation protocol.

use std::net::TcpStream;
use std::time::Instant;

use serde_json::Value;

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::ChatResult;
use crate::error::ErrorProtocol;
use crate::event_log::{AttemptStats, FinalFailure};
use crate::http::{Request, Response};
use crate::reauth::{self, AuthWaitBudget, StatusAction};
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
    let mut auth_wait_budget = AuthWaitBudget::new(state.config.auth_wait_budget());
    let limits = RetryLimits::from_config(&state.config);
    loop {
        if Instant::now() >= deadline {
            return ChatResult::Response(last.deadline_error().response());
        }
        if retry_state.physical_attempts >= state.config.max_physical_attempts {
            return ChatResult::Response(reauth::final_error(&last, &retry_state).response());
        }
        retry_state.record_attempt();
        stats.record_attempt();
        let attempt_started = Instant::now();
        let credential = state.proxy.upstream_credential();
        let mut response = match post_compat(
            &state.config,
            &credential.value,
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
                state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        let status = response.status;
        if status != 200 {
            match reauth::handle_non_success(
                state,
                &mut retry_state,
                &mut last,
                response,
                attempt_started.elapsed(),
                deadline,
                &mut auth_wait_budget,
                credential.api_key,
            ) {
                StatusAction::Retry => continue,
                StatusAction::Final { error, .. } => {
                    return ChatResult::Response(error.response());
                }
            }
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                last.record_transport(failure);
                state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        if serde_json::from_slice::<Value>(&raw)
            .ok()
            .as_ref()
            .and_then(Value::as_object)
            .and_then(|payload| payload.get("data"))
            .and_then(Value::as_array)
            .is_some_and(|data| !data.is_empty())
        {
            state
                .proxy
                .record_business_success("embeddings_completion", Some(200));
        }
        let mut translated = Response::new(status, raw);
        for name in ["content-type", "content-length", "cache-control"] {
            if let Some(value) = response.headers.get(name) {
                translated = translated.with_header(name, value);
            }
        }
        return ChatResult::Response(translated);
    }
}
